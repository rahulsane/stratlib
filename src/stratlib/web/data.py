"""The web app's access to local data: one store for the app's lifetime, and short-lived caches for the reads
every page repeats. Nothing here calls the API."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from ..config import ConfigError, Settings, load_settings
from ..fmp import SharedRateLimiter
from ..presentation import RESULTS, result_name
from ..prices import eod_cutoff
from ..publish import SNAPSHOT
from ..reports import RESEARCH_ROOT
from ..store import Store
from ..strategies import STRATEGIES, latest_screen
from ..technical import load_market


class Workspace:
    """One browser tab's choices that outlive a page: the strategy every page reads, and the stock last looked at."""

    def __init__(self, strategy: str = "canslim"):
        self.strategy = strategy if strategy in STRATEGIES else "canslim"
        self.symbol: str | None = None


class Data:
    """The settings and the store, with cached reads. The store's connection is shared across threads.

    The public app reads a published snapshot read-only; its pages leave out every run, edit and private record."""

    def __init__(self, settings: Settings, *, public: bool = False):
        self.settings = settings
        self.public = public
        self.store = Store(settings.data.db_path, read_only=public)
        self.snapshot = (self.store.document(SNAPSHOT) or {}) if public else {}
        self.research_root = RESEARCH_ROOT
        try:
            self._stamp = settings.path.stat().st_mtime_ns
        except OSError:
            self._stamp = None
        self._timed: dict[str, tuple[float, object]] = {}
        self.closes = lru_cache(maxsize=16)(self._closes)

    def close(self) -> None:
        self.store.close()

    def reload(self) -> None:
        """Read the config file again; raises ConfigError when it cannot be read."""
        self.settings = load_settings(self.settings.path)
        try:
            self._stamp = self.settings.path.stat().st_mtime_ns
        except OSError:
            self._stamp = None
        self.forget()

    def refresh(self) -> bool:
        """Reload the settings when the config file has changed, from this app or elsewhere."""
        try:
            stamp = self.settings.path.stat().st_mtime_ns
        except OSError:
            return False
        if stamp == self._stamp:
            return False
        try:
            self.reload()
        except ConfigError:
            return False
        return True

    def _cached(self, key: str, seconds: float, load):
        saved = self._timed.get(key)
        if saved and time.monotonic() - saved[0] < seconds:
            return saved[1]
        value = load()
        self._timed[key] = (time.monotonic(), value)
        return value

    def forget(self) -> None:
        """Drop cached reads after a write, such as a new screen."""
        self._timed.clear()
        self.closes.cache_clear()

    def screen(self, strategy_id: str, key: str | None = None) -> dict | None:
        """The strategy's latest saved screen, or one saved run by its key."""
        return self.store.document(key) if key else latest_screen(self.store, strategy_id)

    def screen_summaries(self) -> dict[str, dict]:
        """Each strategy's latest saved screen in brief: its date, its candidate count and the first candidates. Only
        the brief is kept, since a CANSLIM screen holds every stock in the universe."""
        def load():
            briefs = {}
            for strategy_id in STRATEGIES:
                report = latest_screen(self.store, strategy_id)
                if report:
                    candidates = report.get("candidates") or []
                    briefs[strategy_id] = {"price_date": report["price_date"], "count": len(candidates),
                                           "top": [{key: c.get(key) for key in ("symbol", "name", "rs")} for c in candidates[:5]]}
            return briefs
        return self._cached("screen_summaries", 900, load)

    def backtest_summaries(self) -> list[dict]:
        """Every saved backtest's headline figures, newest first."""
        def load():
            briefs = []
            for key in self.store.document_keys(RESULTS):
                report = self.store.document(key)
                if report:
                    briefs.append({"strategy": report.get("strategy"), "label": result_name(report),
                                   "start": report["start"], "end": report["end"], "cagr": report["metrics"]["cagr_pct"],
                                   "spy_cagr": report["spy_metrics"]["cagr_pct"],
                                   "max_drawdown": report["metrics"]["max_drawdown_pct"]})
            return briefs
        return self._cached("backtest_summaries", 900, load)

    def engine_result(self, strategy_id: str) -> dict | None:
        """The strategy's newest backtest on the shared engine under the research rules."""
        from ..sim.runs import results_key
        keys = self.store.document_keys(results_key(strategy_id))
        return self.store.document(keys[0]) if keys else None

    def engine_summaries(self) -> dict[str, dict]:
        """Each strategy's newest engine backtest in brief: dates and every period's headline figures."""
        def load():
            from ..sim.runs import latest_results
            briefs = {}
            for strategy_id, doc in latest_results(self.store).items():
                briefs[strategy_id] = {"created_at": doc["created_at"], "data_through": doc["data_through"],
                                       "liquidity": doc.get("liquidity") or {"min_price": 5.0, "min_dollar_volume": 20e6},
                                       "periods": {period: {k: m.get(k) for k in ("start", "end", "trades", "cagr", "max_drawdown",
                                                                                 "sharpe", "win_rate", "total_return")}
                                                   | {"spy_cagr": (m.get("spy") or {}).get("cagr")}
                                                   for period, m in doc["results"].items()}}
            return briefs
        return self._cached("engine_summaries", 300, load)

    def screen_history(self, strategy_id: str) -> list[str]:
        spec = STRATEGIES[strategy_id]
        return self.store.document_keys(f"screen:{spec.id}:{spec.variant}:")

    def snapshot_date(self) -> date | None:
        """The public app's last session: its pages judge freshness against the snapshot, not today."""
        through = self.snapshot.get("prices_through")
        return date.fromisoformat(through) if through else None

    def market(self) -> dict:
        """Market direction from cached index prices through the last completed session."""
        def load():
            cutoff = self.snapshot_date() or eod_cutoff(datetime.now(timezone.utc), self.settings.prices.eod_final_hour_et)
            return load_market(self.store, self.settings.thresholds, cutoff.isoformat())
        return self._cached("market", 300, load)

    def listings(self) -> dict[str, dict]:
        """Name, exchange, sector and industry for each listed stock."""
        return self._cached("listings", 900, lambda: {row["symbol"]: row for row in self.store.universe()})

    def calls_today(self) -> int:
        limiter = SharedRateLimiter(self.settings.data.ratelimit_db_path, self.settings.fmp.calls_per_minute)
        return limiter.calls_on()

    def _closes(self, symbols: tuple[str, ...], through: str) -> dict[str, list[float]]:
        """About six months of closes per symbol."""
        since = (date.fromisoformat(through) - timedelta(days=183)).isoformat()
        points: dict[str, list[tuple[str, float]]] = {}
        for symbol, day, _open, _high, _low, close, _volume in self.store.price_panel_rows(list(symbols), since, through):
            if close is not None:
                points.setdefault(symbol, []).append((day, close))
        return {symbol: [c for _, c in sorted(values)] for symbol, values in points.items()}

    def bars(self, symbol: str, through: str, limit: int):
        return self.store.price_history(symbol, limit=limit, through=through)
