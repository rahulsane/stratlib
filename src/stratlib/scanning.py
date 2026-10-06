"""Live scans for the strategies that come from the research folder.

A scan reads the cached completed daily bars (and, for Episodic Pivot, one FMP earnings-calendar
call), applies the research rules from setups.py to the last completed session and saves a dated
screen in the same store as the CANSLIM screens. It fills no orders and makes no other API calls.

Entries follow the research engine's conventions:
- Buy-stop strategies (Qullamaggie, Minervini) place an order at the pivot after a setup closes. The
  order is live for ``order_life`` sessions, a newer setup on the same stock replaces it, and it
  fills at the pivot or at the open on a gap above it.
- Close-entry strategies (Episodic Pivot, 9/21 EMA pullback) buy at the signal session's close.
- Extra signals are ranked by 63-session return, highest first.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd

from . import setups
from .live_panel import LivePanel
from .prices import ET, eod_cutoff
from .screening import ScreenError

log = logging.getLogger(__name__)

# Rows of the newest sessions that decide the screen; columns are cut to stocks liquid in any of them.
TAIL = 12


class ScanError(ScreenError):
    pass


@dataclass
class ScanInput:
    panel: LivePanel
    params: object
    filter_mask: np.ndarray            # [session] True while the strategy's market filter is on
    meta: dict                         # symbol -> {"name", "sector", "industry"}
    earnings: dict | None = None       # symbol -> release dates, when the strategy needs them
    filter_name: str = "A"
    statements: dict | None = None     # symbol -> cached statement bundle, for the statement-based strategies
    spy_close: np.ndarray | None = None  # forward-filled SPY closes on the panel's sessions
    extra: object = None               # what a scanner's own load() returned


@dataclass
class ScanResult:
    candidates: list = field(default_factory=list)   # actionable, ranked
    triggered: list = field(default_factory=list)    # buy-stops already filled since the setup
    blocked: list = field(default_factory=list)      # setups the market filter held back
    skipped: list = field(default_factory=list)      # setups the strategy's own rules refuse
    members: list = field(default_factory=list)      # symbols a rebalancing strategy keeps holding
    notes: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)      # saved with the screen, such as an index review's dates


def _num(value, digits: int = 6):
    """JSON-safe number: None for missing or non-finite values."""
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if np.isfinite(value) else None


def risk_weight(p, entry: float, stop: float) -> tuple[float | None, float | None]:
    """(risk to the stop in %, initial weight in % of equity) under the research sizing rule.

    shares = risk_pct x equity / (entry - stop), capped at max_position_pct of equity.
    """
    if not (np.isfinite(entry) and np.isfinite(stop)) or stop >= entry or entry <= 0:
        return None, None
    risk = 100 * (entry - stop) / entry
    return risk, min(p.max_position_pct, p.risk_pct / risk * 100)


def _identity(panel, j, meta, row) -> dict:
    symbol = str(panel.symbols[j])
    info = meta.get(symbol, {})
    ret = panel.ret63[row, j]
    return {"symbol": symbol, "name": info.get("name"), "sector": info.get("sector"),
            "industry": info.get("industry"), "close": _num(panel.close[panel.last, j]),
            "ret63_pct": _num(100 * ret, 2) if np.isfinite(ret) else None}


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """63-session return, highest first, then ticker (the research engine's tie-break for slots)."""
    ordered = sorted(candidates, key=lambda c: (c["ret63_pct"] is None, -(c["ret63_pct"] or 0), c["symbol"]))
    for rank, candidate in enumerate(ordered, 1):
        candidate["rank"] = rank
        candidate["order"] = [rank, candidate["symbol"]]
    return ordered


def live_orders(panel, setup: np.ndarray, pivot: np.ndarray, eligible: np.ndarray, mask: np.ndarray,
                order_life: int) -> tuple[list[dict], list[dict]]:
    """Buy-stop orders alive for the next session, and setups the market filter blocked.

    Returns (orders, blocked). An order records its signal row, pivot and, if the pivot has already
    traded through since the setup, the row it triggered on.
    """
    last = panel.last
    latest: dict[int, int] = {}
    blocked: dict[int, int] = {}
    for row in range(max(0, last - order_life + 1), last + 1):
        for j in np.flatnonzero(setup[row] & eligible[row]):
            if mask[row]:
                latest[int(j)] = row       # a newer setup replaces the older order
            else:
                blocked[int(j)] = row
    orders = []
    for j, row in latest.items():
        trigger = float(pivot[row, j])
        fill = None
        for u in range(row + 1, last + 1):
            o, h = panel.open[u, j], panel.high[u, j]
            if (np.isfinite(o) and o > trigger) or (np.isfinite(h) and h > trigger):
                fill = u
                break
        orders.append({"j": j, "row": row, "pivot": trigger, "triggered_row": fill})
    blocks = [{"j": j, "row": row, "pivot": float(pivot[row, j])} for j, row in blocked.items() if j not in latest]
    return orders, blocks


def _blocked_entry(panel, meta, item, filter_name) -> dict:
    return {**_identity(panel, item["j"], meta, item["row"]), "signal_date": str(panel.dates[item["row"]]),
            "pivot": _num(item["pivot"]), "reason": f"Market filter {filter_name} was off on the signal date"}


# ----------------------------------------------------------------------------------------------
# Strategies

class Scanner:
    id = ""
    needs_api = False                  # a run with a client makes calls (earnings calendar, statements)
    needs_statements = False           # reads company statements
    entry = "stop"                     # stop: buy-stop at a pivot; close: buy at the signal close
    candidate_columns: list[tuple[str, str]] = []   # (key, label) shown beside the common ones
    keep_all = False                   # keep every loaded stock, not only those liquid in the newest sessions
    # A scanner with its own universe defines load(store, client, sessions, today, *, workers, warnings, progress),
    # returning (symbols to load, inputs for ScanInput.extra); it may make API calls when client is set.
    load = None

    def sessions_needed(self, p) -> int:
        raise NotImplementedError

    def scan(self, inp: ScanInput) -> ScanResult:
        raise NotImplementedError


class QullamaggieScanner(Scanner):
    id = "qullamaggie"
    candidate_columns = [("pivot", "Buy stop, USD"), ("stop", "Stop if filled, USD"), ("risk_pct", "Risk to stop, %"),
                         ("adr_pct", "20-day range, %"), ("sessions_left", "Sessions left"),
                         ("prior_move_pct", "Prior move, %"), ("days_since_high", "Days since high")]

    def sessions_needed(self, p) -> int:
        return p.lookback + p.consolidation_max + p.sma_slow + 40

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        found = setups.qullamaggie_setups(panel, p)
        last = panel.last
        orders, blocks = live_orders(panel, found["setups"], found["pivot"], panel.eligible, inp.filter_mask, p.order_life)
        result = ScanResult(blocked=[_blocked_entry(panel, inp.meta, b, inp.filter_name) for b in blocks])
        for order in orders:
            j, row = order["j"], order["row"]
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[row]),
                    "pivot": _num(order["pivot"]), "adr_pct": _num(found["adr"][last, j], 2),
                    "sessions_left": p.order_life - (last - row),
                    "prior_move_pct": _num(100 * (found["top"][row, j] / found["prior_low"][row, j] - 1), 1),
                    "days_since_high": int(found["since"][row, j])}
            if order["triggered_row"] is not None:
                day = order["triggered_row"]
                result.triggered.append({**item, "entry_date": str(panel.dates[day]),
                                         "entry_price": _num(max(order["pivot"], panel.open[day, j])
                                                             if np.isfinite(panel.open[day, j]) else order["pivot"])})
                continue
            stop = float(panel.low[last, j])       # the session before the next session's entry
            risk, weight = risk_weight(p, order["pivot"], stop)
            adr = found["adr"][last, j]
            item.update(stop=_num(stop), risk_pct=_num(risk, 2), weight_pct=_num(weight, 2))
            if risk is None or not np.isfinite(adr) or (p.max_stop_adr is not None and risk > p.max_stop_adr * adr):
                item["reason"] = (f"Stop is {risk:.1f}% below the pivot, more than {p.max_stop_adr:g} x the "
                                  f"{adr:.1f}% average daily range" if risk is not None and np.isfinite(adr)
                                  else "No usable stop")
                result.skipped.append(item)
                continue
            result.candidates.append(item)
        result.candidates = rank_candidates(result.candidates)
        result.counts = {"setups": int(found["setups"][last].sum()), "live_orders": len(orders)}
        return result


class MinerviniScanner(Scanner):
    id = "minervini"
    candidate_columns = [("pivot", "Buy stop, USD"), ("stop", "Stop if filled, USD"), ("risk_pct", "Risk to stop, %"),
                         ("rs", "RS, percentile"), ("contractions", "Contractions"), ("final_depth_pct", "Last pullback, %"),
                         ("volume_needed", "Breakout volume needed"), ("sessions_left", "Sessions left")]

    def sessions_needed(self, p) -> int:
        return 504

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        template, rs = setups.minervini_template(panel, p)
        scan = setups.minervini_vcp(panel, template, p)
        last = panel.last
        orders, blocks = live_orders(panel, scan["setup"], scan["pivot"], panel.eligible, inp.filter_mask, p.order_life)
        avg50 = setups.rolling(panel.volume, 50, "mean")
        result = ScanResult(blocked=[_blocked_entry(panel, inp.meta, b, inp.filter_name) for b in blocks])
        for order in orders:
            j, row = order["j"], order["row"]
            peak = int(scan["peak_at"][row, j])
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[row]),
                    "pivot": _num(order["pivot"]), "rs": _num(rs[row, j], 1),
                    "contractions": int(scan["contractions"][row, j]),
                    "final_depth_pct": _num(100 * scan["final_depth"][row, j], 1),
                    "sessions_left": p.order_life - (last - row)}
            if order["triggered_row"] is not None:
                day = order["triggered_row"]
                average = avg50[day - 1, j] if day > 0 else np.nan
                volume = panel.volume[day, j]
                kept = bool(np.isfinite(average) and np.isfinite(volume) and volume >= p.breakout_volume * average)
                result.triggered.append({**item, "entry_date": str(panel.dates[day]),
                                         "entry_price": _num(max(order["pivot"], panel.open[day, j])
                                                             if np.isfinite(panel.open[day, j]) else order["pivot"]),
                                         "volume_confirmed": kept})
                continue
            lows = panel.low[peak + 1:last + 1, j] if peak >= 0 else np.array([])
            if not np.isfinite(lows).any():
                item["reason"] = "No usable stop"
                result.skipped.append(item)
                continue
            stop = max(float(np.nanmin(lows)), order["pivot"] * (1 - p.max_stop_pct / 100))
            risk, weight = risk_weight(p, order["pivot"], stop)
            item.update(stop=_num(stop), risk_pct=_num(risk, 2), weight_pct=_num(weight, 2),
                        volume_needed=_num(p.breakout_volume * avg50[last, j], 0))
            result.candidates.append(item)
        result.candidates = rank_candidates(result.candidates)
        result.counts = {"trend_template": int(template[last].sum()), "setups": int(scan["setup"][last].sum()),
                         "live_orders": len(orders)}
        return result


class EpisodicPivotScanner(Scanner):
    id = "episodic_pivot"
    needs_api = True
    entry = "close"
    candidate_columns = [("gap_pct", "Gap, %"), ("volume_multiple", "Volume, x average"), ("stop", "Stop (day's low), USD"),
                         ("risk_pct", "Risk to stop, %"), ("earnings_date", "Earnings date")]

    def sessions_needed(self, p) -> int:
        return p.volume_sessions + p.neglect_sessions + 60

    @staticmethod
    def release_dates(panel, dates: list[str], last: int) -> list[str]:
        """Releases that make the last session 'the first after earnings', as the research mask counts them."""
        found = []
        for d in dates:
            t = int(np.searchsorted(panel.dates, d))
            if t == last or (t == last - 1 and panel.dates[t] == d):
                found.append(d)
        return sorted(found)

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        last = panel.last
        result = ScanResult()
        verified = None
        if p.earnings and inp.earnings is not None:
            verified = setups.episodic_signals(panel, p, setups.earnings_mask(panel, inp.earnings))
        elif p.earnings:
            result.notes.append("Earnings dates were not fetched (offline run). Gap signals are listed as unverified "
                                "and are not candidates; run the screen with the earnings refresh to confirm them.")
        base = setups.episodic_signals(panel, replace(p, earnings=False), None)     # gap, volume, neglect
        entries = base & setups.strong_close(panel, p.close_in_range) & setups.prior_liquidity(panel)
        prev_close = panel.close_ff[last - 1]
        average = pd.DataFrame(panel.volume).rolling(p.volume_sessions, min_periods=p.volume_sessions).mean().to_numpy()[last - 1]
        for j in np.flatnonzero(entries[last]):
            entry, stop = float(panel.close[last, j]), float(panel.low[last, j])
            risk, weight = risk_weight(p, entry, stop)
            released = self.release_dates(panel, (inp.earnings or {}).get(str(panel.symbols[j]), []), last)
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[last]),
                    "gap_pct": _num(100 * (panel.open[last, j] / prev_close[j] - 1), 1),
                    "volume_multiple": _num(panel.volume[last, j] / average[j], 1), "stop": _num(stop),
                    "risk_pct": _num(risk, 2), "weight_pct": _num(weight, 2),
                    "earnings_date": released[-1] if released else None}
            if p.earnings and verified is None:
                result.skipped.append({**item, "reason": "Unverified: earnings dates were not fetched"})
            elif p.earnings and not verified[last, j]:
                result.skipped.append({**item, "reason": "No earnings release in the days before the gap"})
            elif not inp.filter_mask[last]:
                result.blocked.append({**item, "reason": f"Market filter {inp.filter_name} is off"})
            else:
                result.candidates.append(item)
        result.candidates = rank_candidates(result.candidates)
        result.counts = {"gap_and_volume": int(base[last].sum()), "strong_close_and_liquid": int(entries[last].sum())}
        return result


class EmaPullbackScanner(Scanner):
    id = "ema_pullback"
    entry = "close"
    candidate_columns = [("ema_fast", "9-day EMA, USD"), ("ema_slow", "21-day EMA, USD"), ("stop", "Stop, USD"),
                         ("risk_pct", "Risk to stop, %")]

    def sessions_needed(self, p) -> int:
        return max(p.sma_long, p.high_lookback) + 60

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        found = setups.ema_pullback_setups(panel, p)
        last = panel.last
        result = ScanResult()
        for j in np.flatnonzero(found["setups"][last] & panel.eligible[last]):
            slow = found["ema_slow"][last, j]
            entry, stop = float(panel.close[last, j]), float(slow * (1 - p.stop_pct / 100))
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[last]),
                    "ema_fast": _num(found["ema_fast"][last, j], 2), "ema_slow": _num(slow, 2)}
            risk, weight = risk_weight(p, entry, stop)
            item.update(stop=_num(stop), risk_pct=_num(risk, 2), weight_pct=_num(weight, 2))
            if risk is None:
                item["reason"] = "The stop would be at or above the close"
                result.skipped.append(item)
            elif not inp.filter_mask[last]:
                result.blocked.append({**item, "reason": f"Market filter {inp.filter_name} is off"})
            else:
                result.candidates.append(item)
        result.candidates = rank_candidates(result.candidates)
        result.counts = {"setups": int((found["setups"][last] & panel.eligible[last]).sum())}
        return result


SCANNERS: dict[str, Scanner] = {s.id: s for s in (QullamaggieScanner(), MinerviniScanner(), EpisodicPivotScanner(),
                                                     EmaPullbackScanner())}

# ----------------------------------------------------------------------------------------------
# Running a scan


def _filter_need(code: str) -> int:
    return {"A": 1, "B": 51, "C": 21, "D": 300, "E": 300, "F": 300}[code]


def _filled_close(store, symbol: str, sessions: list[str]) -> np.ndarray:
    bars = {b.date: b.close for b in store.price_history(symbol, since=sessions[0], through=sessions[-1])}
    return pd.Series([bars.get(d, np.nan) for d in sessions]).ffill(limit=5).to_numpy()


def market_filter_state(store, panel, sessions: list[str], code: str) -> dict:
    """The filter's mask over the panel's sessions and a readable summary of its latest state."""
    from .strategy_params import MARKET_FILTERS
    spy, qqq = _filled_close(store, "SPY", sessions), _filled_close(store, "QQQ", sessions)
    found = setups.market_filter_masks(panel, spy, qqq)
    mask = found["masks"][code]
    detail = {"A": "no filter", "B": f"SPY {spy[-1]:,.2f} against its 50-day average {found['spy_sma50'][-1]:,.2f}",
              "C": "QQQ 10-day average against its 20-day average",
              "D": f"{100 * found['breadth'][-1]:.0f}% of liquid stocks above their 50-day average",
              "E": f"new highs minus new lows, 10-day average {found['net_highs_lows'][-1]:+.1f}",
              "F": f"SPY above its 50-day average and {100 * found['breadth'][-1]:.0f}% of stocks above theirs"}[code]
    return {"mask": mask, "state": {"code": code, "label": MARKET_FILTERS[code], "on": bool(mask[-1]), "detail": detail}}


def quick_filter(store, code: str, as_of: str) -> dict | None:
    """Filters A to C from cached SPY and QQQ bars alone; D to F need the whole universe, so a scan saves them."""
    from .strategy_params import MARKET_FILTERS
    if code == "A":
        return {"code": "A", "label": MARKET_FILTERS["A"], "on": True, "detail": "no filter"}
    if code == "B":
        bars = store.price_history("SPY", limit=50, through=as_of)
        if len(bars) < 50:
            return None
        close, average = bars[-1].close, sum(b.close for b in bars) / 50
        return {"code": "B", "label": MARKET_FILTERS["B"], "on": close > average,
                "detail": f"SPY {close:,.2f} against its 50-day average {average:,.2f}"}
    if code == "C":
        bars = store.price_history("QQQ", limit=20, through=as_of)
        if len(bars) < 20:
            return None
        fast, slow = sum(b.close for b in bars[-10:]) / 10, sum(b.close for b in bars) / 20
        return {"code": "C", "label": MARKET_FILTERS["C"], "on": fast > slow,
                "detail": f"QQQ 10-day average {fast:,.2f} against its 20-day average {slow:,.2f}"}
    return None


def earnings_dates(client, as_of: str) -> dict[str, list[str]]:
    """Release dates from the FMP earnings calendar for the days around the last session."""
    end = date.fromisoformat(as_of) + timedelta(days=1)
    rows = client.earnings_calendar(end - timedelta(days=10), end)
    if not isinstance(rows, list):
        raise ScanError("Unexpected earnings calendar response; no result was saved.")
    dates: dict[str, list[str]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("symbol") and row.get("date"):
            dates.setdefault(row["symbol"], []).append(str(row["date"])[:10])
    return dates


def run_scan(store, settings, client=None, *, strategy_id: str, now: datetime | None = None,
             progress: Callable[[str, int, int, int], None] | None = None) -> dict:
    """Scan the cached market for one strategy and save a dated screen. ``client=None`` is cache-only."""
    from .strategies import current_rules, strategy
    spec = strategy(strategy_id)
    scanner = SCANNERS[strategy_id]
    params = settings.strategies[strategy_id]
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    cutoff = eod_cutoff(now, settings.prices.eod_final_hour_et)
    if not scanner.needs_api:
        client = None                      # price-only scans make no calls
    initial_calls = client.stats.api_calls if client else 0

    def update(message, done, total):
        if progress:
            progress(message, done, total, client.stats.api_calls - initial_calls if client else 0)

    needed = max(scanner.sessions_needed(params), _filter_need(params.market_filter))
    reference = store.price_history("SPY", limit=needed, through=cutoff.isoformat())
    if len(reference) < needed:
        raise ScreenError(f"{spec.name} needs {needed} completed sessions of SPY history, and {len(reference)} are cached. "
                          "Run stratlib backfill.")
    sessions = [b.date for b in reference]
    as_of = sessions[-1]
    universe = store.universe()
    if not universe:
        raise ScreenError("No stored universe. Run stratlib backfill first.")
    warnings, extra = [], None
    symbols = [s["symbol"] for s in universe]
    if scanner.load is not None:
        symbols, extra = scanner.load(store, client, sessions, now.astimezone(ET).date(), workers=settings.fmp.max_workers,
                                      warnings=warnings, progress=update)
    update("Loading prices", 0, len(symbols))
    everything = LivePanel.load(store, symbols, sessions, min_price=params.min_price,
                                min_dollar_volume=params.min_dollar_volume_m * 1e6)
    if not everything.valid[-1].any():
        raise ScreenError(f"No stored prices on {as_of}. Run stratlib backfill.")
    # Liquid in any of the newest sessions; the rules' ranks and breadth see the same eligible stocks.
    panel = everything if scanner.keep_all else everything.select(np.flatnonzero(everything.eligible[-TAIL:].any(axis=0)))
    update("Applying the market filter", 0, 1)
    filt = market_filter_state(store, everything, sessions, params.market_filter)
    if (cutoff - date.fromisoformat(as_of)).days > 4:
        warnings.append(f"Stored prices are stale. Results use {as_of}; run the daily backfill.")
    stale = int((everything.valid[-2] & ~everything.valid[-1]).sum())
    if stale:
        warnings.append(f"{stale} stocks have no bar on {as_of} and cannot signal.")
    earnings = None
    if strategy_id == "episodic_pivot" and client is not None:
        update("Fetching earnings dates", 0, 1)
        earnings = earnings_dates(client, as_of)
    meta = {s["symbol"]: {"name": s["name"], "sector": s["sector"], "industry": s["industry"]} for s in universe}
    statements = None
    if scanner.needs_statements:
        from .fundamental_scans import load_statements, refresh_statements
        wanted = [str(sym) for sym in panel.symbols[panel.eligible[-1]]
                  if (meta.get(str(sym), {}).get("sector") or "") != "Financial Services"]
        if client is not None:
            refreshed = refresh_statements(store, client, wanted, now.astimezone(ET).date(),
                                           workers=settings.fmp.max_workers, progress=update)
            if refreshed["errors"]:
                warnings.append(f"{refreshed['errors']} companies' statements could not be fetched; their cached statements were kept.")
        statements = {sym: load_statements(store, sym) for sym in wanted}
        missing = sum(doc is None for doc in statements.values())
        if missing:
            warnings.append(f"{missing} of {len(wanted)} liquid non-financial stocks have no cached statements and cannot be "
                            "evaluated" + ("." if client else "; run the screen with the statement refresh to fetch them."))
        elif client is None:
            warnings.append("Cache-only run: statements were not refreshed.")
    run_id = store.start_run("scan", {"strategy": strategy_id, "cache_only": client is None})
    try:
        update(f"Applying the {spec.name} rules", 0, 1)
        result = scanner.scan(ScanInput(panel, params, filt["mask"], meta, earnings, params.market_filter, statements,
                                        _filled_close(store, "SPY", sessions) if scanner.needs_statements else None,
                                        extra))
        snapshot = current_rules(settings, strategy_id)
        report = {
            "run_id": run_id, "created_at": now.isoformat(), "price_date": as_of, "fundamental_as_of": None,
            "universe_count": len(universe), "evaluated_count": len(panel.symbols), "eligible_count": int(panel.eligible[-1].sum()),
            "sample": None, "phase": "scan", "cache_only": client is None, "warnings": warnings + result.notes,
            "api_calls": client.stats.api_calls - initial_calls if client else 0,
            "elapsed_seconds": time.monotonic() - started, "market_filter": filt["state"],
            "candidates": result.candidates, "triggered": result.triggered, "blocked": result.blocked,
            "skipped": result.skipped, "members": result.members, "counts": result.counts, "summary": result.summary,
            "rows": [],
            "strategy_id": strategy_id, "variant_id": spec.variant, "rule_snapshot": snapshot,
        }
        report = store.save_screen(report)
        store.finish_run(run_id, {k: v for k, v in report.items()
                                  if k not in {"candidates", "triggered", "blocked", "skipped", "members"}})
        log.info("%s scan finished: %d candidates, %d liquid stocks, %.1fs", spec.name, len(result.candidates),
                 len(panel.symbols), report["elapsed_seconds"])
        return report
    except BaseException as exc:
        store.finish_run(run_id, {"status": "failed", "error": type(exc).__name__})
        raise


from . import fundamental_scans, index_scans  # noqa: E402,F401  (registers the statement-based and index scanners)
