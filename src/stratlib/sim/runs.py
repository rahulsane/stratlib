"""Backtests of every workspace strategy under one set of ground rules, so they compare.

Each strategy runs with the settings in config.yaml over the research periods (in-sample 2016-2021, out-of-sample
2022 onward, and combined), on one panel: today's US common stocks and those delisted since 2016, the universe the
screens use. The liquidity floor is each scan strategy's own (min_price, min_dollar_volume_m) and FLOOR for CANSLIM
and Trend Leaders; the panel is built at the lowest of them, and a stricter strategy gets its own mask. The scan strategies are the research strategy classes; CANSLIM, Trend Leaders and Nash are the app's
(workspace.py). MSCI GARP copies an index, topping up and trimming every holding at each review, which the
trade-by-trade engine cannot do; msci_garp.py keeps its account under the same rules and measures it with the same
metrics. Results are saved as ``backtest:engine:<strategy>:<time>`` in the research results format.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict
from datetime import datetime, timezone

import numpy as np

from ..backtest_approx import PREPARED, breakout_signals, saved_leader_orders
from ..exits import exit_rule
from ..market_direction import market_direction
from ..presentation import rules_text
from ..strategies import INDEXES, STRATEGIES, current_rules, holding_limit
from .data import benchmarks, classifications, earnings_dates, rebalance_rows, stock_panel
from .engine import PERIODS, Rules, metrics, set_up, simulate, spy_series
from .panel import STUDY_FROM
from .workspace import (Breakout, CanslimBreakouts, NashQuality, TrendLeaders, checklist_passes, nash_passers)

RESULTS = "backtest:engine:"
# The liquidity floor for the strategies without one in their settings (CANSLIM and Trend Leaders): an as-traded
# close of at least $5 and a 20-session average dollar volume of at least $5M on the signal day.
FLOOR = (5.0, 5e6)
# The trade fields a saved result keeps: as-traded prices, as a chart of the time shows them.
TRADE_FIELDS = ("ticker", "signal_date", "entry_date", "exit_date", "entry_price_as_traded", "exit_price_as_traded",
                "shares", "position_value", "return_pct", "r", "pnl", "holding_sessions", "exit_reason")


def results_key(strategy_id: str) -> str:
    return f"{RESULTS}{strategy_id}:"


def trade_table(trades) -> dict:
    """A period's trades as columns and rows with rounded numbers: some strategies make tens of thousands."""
    return {"columns": list(TRADE_FIELDS),
            "rows": [[round(v, 4) if isinstance(v, float) else v for v in (getattr(t, f) for f in TRADE_FIELDS)]
                     for t in trades]}


def trade_rows(table: dict) -> list[dict]:
    return [dict(zip(table["columns"], row)) for row in table.get("rows", [])]


def latest_results(store) -> dict[str, dict]:
    """Each strategy's newest saved engine backtest."""
    found = {}
    for strategy_id in STRATEGIES:
        keys = store.document_keys(results_key(strategy_id))
        if keys and (doc := store.document(keys[0])):
            found[strategy_id] = doc
    return found


def through_date(store) -> str:
    """The last session every strategy can run to: the newest SPY close, or the end of the approximate method's
    prepared data if that is earlier (CANSLIM's and Trend Leaders' signals need it)."""
    last = store.price_history("SPY", limit=1)[-1].date
    prepared = store.document(PREPARED)
    return min(last, prepared["end"]) if prepared else last


def floor(strategy_id: str, settings) -> tuple[float, float]:
    """The strategy's liquidity floor: (minimum as-traded close, minimum 20-session average dollar volume)."""
    p = settings.strategies.get(strategy_id)
    return (float(p.min_price), float(p.min_dollar_volume_m) * 1e6) if p is not None else FLOOR


class Inputs:
    """What the strategies read, built once per run and only when a strategy needs it."""

    def __init__(self, store, settings, through: str, *, workers: int = 1, note=None, panel=None):
        self.store, self.settings, self.through, self.workers = store, settings, through, workers
        self.note = note or (lambda message, done, total: None)
        self.panel = panel if panel is not None else stock_panel(store, settings)
        self.bench = benchmarks(store)
        self._cache: dict = {}

    def cached(self, name, build):
        if name not in self._cache:
            self._cache[name] = build()
        return self._cache[name]

    def exposure(self) -> np.ndarray:
        """The market's allowed exposure at each panel session's close (the CANSLIM and Trend Leaders ladder)."""
        def build():
            t = self.settings.thresholds
            histories = {s: self.store.price_history(s, through=self.through) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}
            known = {row["date"]: row["exposure"] for row in market_direction(histories, t, as_of=self.through)["history"]}
            return np.array([known.get(str(d)) or 0.0 for d in self.panel.dates], dtype=float)
        return self.cached("exposure", build)

    def masks(self) -> dict:
        from .market_filters import compute
        return self.cached("masks", lambda: compute(self.panel)["masks"])

    def classes(self) -> dict:
        return self.cached("classes", lambda: classifications(self.store))

    def rows(self) -> list[int]:
        return self.cached("rows", lambda: rebalance_rows(self.panel))

    def eligible(self, min_price: float, min_dollar_volume: float) -> np.ndarray:
        """Stocks that clear a liquidity floor: the panel's own mask when the floor is the panel's. RS ranks and
        market breadth still use the panel's floor, so a stricter strategy only narrows what it may buy."""
        panel = self.panel
        if (min_price, min_dollar_volume) == (panel.min_price, panel.min_dollar_volume):
            return panel.eligible
        return self.cached(("eligible", min_price, min_dollar_volume), lambda: panel.liquid(
            min_price, min_dollar_volume) & (panel.kind == "stock")[None, :])

    def column(self, symbol: str) -> int | None:
        return self.panel.index.get(symbol)

    def row(self, day: str) -> int | None:
        return self.panel.day.get(day)


# ----------------------------------------------------------------------------------------------
# Each strategy's engine configuration: (factory, parameters, rules)


def _research_params(defaults: dict, p, **extra) -> dict:
    """The research strategy's defaults with the live settings that share their names, then extra."""
    return {**defaults, **{k: v for k, v in asdict(p).items() if k in defaults}, **extra}


def _scan_rules(strategy_id: str, settings) -> Rules:
    p = settings.strategies[strategy_id]
    return Rules(risk_pct=p.risk_pct, max_position_pct=p.max_position_pct,
                 max_positions=holding_limit(strategy_id, settings, settings.backtest))


def _filtered(factory, inputs: Inputs, code: str):
    """The research's market-filter wrapper: the strategy signals only on sessions the filter allows."""
    if code == "A":
        return factory
    from .market_filters import Filtered
    inner, shared, mask = factory(), {}, inputs.masks()[code]
    return lambda: Filtered(inner, mask, code, shared)


def configure(strategy_id: str, inputs: Inputs):
    settings = inputs.settings
    t, portfolio = settings.thresholds, settings.backtest
    if strategy_id == "canslim":
        def signals():
            found, missing = {}, 0
            by_day = breakout_signals(inputs.store, settings, STUDY_FROM, inputs.through, workers=inputs.workers,
                                      progress=inputs.note)
            for day, items in by_day.items():
                row = inputs.row(day)
                for s in items:
                    j = inputs.column(s.symbol)
                    if row is None or j is None:
                        missing += 1
                        continue
                    found.setdefault(row, []).append(Breakout(j, s.symbol, s.pivot, s.rs, s.breakout_date or s.date))
            return found, missing
        found, _ = inputs.cached("canslim", signals)
        params = {"max_holdings": portfolio.max_holdings, "raise_cash": portfolio.raise_cash,
                  **{k: getattr(t, k) for k in ("stop_loss_pct", "profit_target_pct", "fast_gain_pct", "fast_gain_weeks",
                                                "minimum_hold_weeks", "buy_zone_max_pct")}}
        exposure = inputs.exposure()
        return (lambda: CanslimBreakouts(found, exposure)), params, Rules(max_positions=portfolio.max_holdings,
                                                                          max_position_pct=100.0)
    if strategy_id == "trend":
        def ranking():
            orders = saved_leader_orders(inputs.store, settings, STUDY_FROM, inputs.through, strategy="trend",
                                         portfolio=portfolio, progress=inputs.note, workers=inputs.workers)
            reach = portfolio.leaders_rank_buffer + portfolio.max_holdings    # as run_leaders: deeper ranks never buy
            found = {}
            for day, symbols in orders["orders"].items():
                row = inputs.row(day)
                if row is not None:
                    found[row] = [j for j in map(inputs.column, symbols[:reach]) if j is not None]
            return found
        found = inputs.cached("trend", ranking)
        params = {"max_holdings": portfolio.max_holdings, "loss_cap_pct": portfolio.trend_loss_cap_pct,
                  "long_ma_sessions": t.long_ma_sessions}
        exposure = inputs.exposure()
        return (lambda: TrendLeaders(found, exposure)), params, Rules(max_positions=portfolio.max_holdings,
                                                                      max_position_pct=100.0)
    p = settings.strategies[strategy_id]
    if strategy_id == "qullamaggie":
        from .strategies.qullamaggie import DEFAULTS, Qullamaggie
        return _filtered(Qullamaggie, inputs, p.market_filter), _research_params(DEFAULTS, p), _scan_rules(strategy_id, settings)
    if strategy_id == "minervini":
        from .strategies.minervini import DEFAULTS, Minervini
        params = _research_params(DEFAULTS, p, exit=exit_rule(strategy_id, p))
        return _filtered(Minervini, inputs, p.market_filter), params, _scan_rules(strategy_id, settings)
    if strategy_id == "episodic_pivot":
        from .strategies.episodic_pivot import DEFAULTS, EpisodicPivot
        dates = inputs.cached("earnings", lambda: earnings_dates(inputs.store, inputs.panel)) if p.earnings else None
        # The corrected research rules the live screen follows: liquidity measured before the signal day, and no
        # stock whose split adjustment varies over 100-fold since 2016 (a data guard only the panel can apply).
        params = _research_params(DEFAULTS, p, exit=exit_rule(strategy_id, p), liquidity="prior",
                                  max_split_multiple=100, split_window_start=STUDY_FROM, min_price=p.min_price,
                                  min_dollar_volume=p.min_dollar_volume_m * 1e6)
        return _filtered(lambda: EpisodicPivot(dates), inputs, p.market_filter), params, _scan_rules(strategy_id, settings)
    if strategy_id == "ema_pullback":
        from .strategies.ema_pullback import DEFAULTS, EmaPullback
        return _filtered(EmaPullback, inputs, p.market_filter), _research_params(DEFAULTS, p), _scan_rules(strategy_id, settings)
    if strategy_id == "tt_checklist":
        from .strategies.tt_checklist_top10 import RankedChecklist
        from .strategies.tt_checklist_trend import TrendGatedChecklist
        passes = inputs.cached("checklist", lambda: checklist_passes(
            inputs.store, inputs.panel, p, inputs.rows(), inputs.classes(), inputs.eligible(*floor(strategy_id, settings))))
        params = {"stop_pct": p.stop_pct, "equal_weight": True, "top_n": p.top_n}
        cls = RankedChecklist
        if p.trend_gate != "none":
            cls, params = TrendGatedChecklist, {**params, "trend_sessions": p.trend_sessions,
                                                "market_gate": p.trend_gate in ("market", "both"),
                                                "stock_gate": p.trend_gate in ("stock", "both")}

        def factory():
            strategy = cls()
            strategy.passes = passes
            return strategy
        return _filtered(factory, inputs, p.market_filter), params, _scan_rules(strategy_id, settings)
    if strategy_id == "nash_quality":
        passers = inputs.cached("nash", lambda: nash_passers(
            inputs.store, inputs.panel, p, inputs.rows(), inputs.classes(), inputs.eligible(*floor(strategy_id, settings))))
        rules = Rules(risk_pct=p.risk_pct, max_position_pct=100.0, max_positions=p.slots)
        return _filtered(lambda: NashQuality(passers), inputs, p.market_filter), {"slots": p.slots}, rules
    raise ValueError(f"Unknown strategy: {strategy_id}")


# ----------------------------------------------------------------------------------------------
# Running and saving


def json_safe(value):
    """Plain JSON: numpy scalars become Python numbers, and NaN or infinity become None."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return [json_safe(v) for v in value.tolist()]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    return value


def run_strategy(strategy_id: str, inputs: Inputs) -> dict:
    """All three research periods for one strategy, saved and returned."""
    started = time.monotonic()
    spec, settings, panel = STRATEGIES[strategy_id], inputs.settings, inputs.panel
    inputs.note(f"Preparing {spec.name}", 0, len(PERIODS))
    factory, params, rules = configure(strategy_id, inputs)
    min_price, min_dollar_volume = floor(strategy_id, settings)
    mask = inputs.eligible(min_price, min_dollar_volume)
    results, trades = {}, {}
    for k, (period, (start, end)) in enumerate(PERIODS.items()):
        inputs.note(f"Testing {spec.name}, {period.replace('_', ' ')}", k, len(PERIODS))
        strategy = set_up(factory, panel, params)
        if mask is not panel.eligible:
            strategy.eligible_mask = mask
        if hasattr(strategy, "begin_period"):
            strategy.begin_period(period)
        run = simulate(panel, strategy, start, end, rules)
        spy = spy_series(panel, inputs.bench["spy_dividends"], start, end or str(panel.dates[-1]))
        metric = metrics(run, spy, inputs.bench["tbill3m"])
        extra = getattr(strategy, "counts", None)
        if isinstance(extra, dict):
            metric["counts"] = {**metric["counts"], **extra}
        results[period] = metric
        trades[period] = trade_table(run["trades"])
    created = datetime.now(timezone.utc).isoformat()
    doc = json_safe({
        "name": strategy_id, "strategy_id": strategy_id, "variant_id": spec.variant, "strategy": spec.name,
        "label": spec.variant_name, "description": f"**{spec.name} / {spec.variant_name}.** {rules_text(spec, settings)}",
        "params": params, "rules": asdict(rules), "rule_snapshot": current_rules(settings, strategy_id),
        "liquidity": {"min_price": min_price, "min_dollar_volume": min_dollar_volume},
        "data_through": str(panel.dates[-1]), "universe": "US common stocks, including those delisted since 2016",
        "periods": {k: list(v) for k, v in PERIODS.items()}, "results": results, "trades": trades,
        "created_at": created, "elapsed_seconds": time.monotonic() - started,
    })
    inputs.store.save_document(results_key(strategy_id) + created, doc)
    return doc


def run_all(store, settings, strategy_ids: list[str] | None = None, *, workers: int = 1, progress=None) -> dict:
    """Backtest the given strategies (all of them by default), the engine's on one shared panel. {strategy id: saved
    result}."""
    note = progress or (lambda message, done, total: None)
    through, strategy_ids = through_date(store), strategy_ids or list(STRATEGIES)
    on_engine = [strategy_id for strategy_id in strategy_ids if strategy_id not in INDEXES]
    done = {}
    if on_engine:
        floors = [floor(strategy_id, settings) for strategy_id in on_engine]
        note(f"Building the price panel through {through}", 0, 1)
        inputs = Inputs(store, settings, through, workers=workers, note=note,
                        panel=stock_panel(store, settings, through=through, min_price=min(f[0] for f in floors),
                                          min_dollar_volume=min(f[1] for f in floors)))
        done = {strategy_id: run_strategy(strategy_id, inputs) for strategy_id in on_engine}
    if "msci_garp" in strategy_ids:
        from .msci_garp import run_backtest
        done["msci_garp"] = run_backtest(store, settings, through, progress=note)
    return {strategy_id: done[strategy_id] for strategy_id in strategy_ids}
