"""Cached market loading and pure N/M criteria shared by screening and the GUI."""

from datetime import date, timedelta

from .bases import detect_base, history_weeks
from .config import Thresholds
from .market_direction import CONFIRMED, CORRECTION, INDEX_RULES, INDEXES, market_direction
from .parallel import run_tasks
from .scoring import Criterion

# Below this many stocks, starting worker processes costs more than it saves.
PARALLEL_MIN_STOCKS = 200
BATCH_STOCKS = 50

TECHNICAL_CRITERIA = [
    ("n_position", "N", "Price above base pivot"),
    ("n_volume", "N", "Breakout volume"),
    ("m_direction", "M", "Market direction"),
]


def load_market(store, t: Thresholds, as_of: str) -> dict:
    histories = {symbol: store.price_history(symbol, through=as_of)
                 for symbol in [*INDEXES, "SPY", "QQQ"]}
    return market_direction(histories, t, as_of=as_of)


def stock_setup(store, symbol: str, as_of: str, t: Thresholds, market: dict) -> tuple[list, dict]:
    sessions = [d["date"] for d in market["history"]]
    # Three chart years, or the configured base search if longer. Preserve all
    # calendar sessions in that interval so a partial first week cannot count.
    since = (date.fromisoformat(as_of) - timedelta(weeks=max(158, history_weeks(t) + 11))).isoformat()
    sessions = [d for d in sessions if d >= since]
    bars = store.price_history(symbol, since=since, through=as_of)
    corrections = frozenset(d["date"] for d in market["history"] if d["state"] == CORRECTION)
    return bars, detect_base(bars, as_of, t, correction_dates=corrections, sessions=sessions or None)


def stock_bases(store, symbols: list[str], as_of: str, t: Thresholds, market: dict, *, workers: int = 1,
                progress=None) -> dict[str, dict]:
    """Each symbol's base from stock_setup, spread over ``workers`` processes when the list is long (see
    parallel.py before raising it from a script). Each stock's base depends only on its own bars, so the result
    is the same however the work is split. ``progress(done, total)`` follows the stocks finished.
    """
    batches = [symbols[i:i + BATCH_STOCKS] for i in range(0, len(symbols), BATCH_STOCKS)]
    report = progress and (lambda done, total: progress(min(done * BATCH_STOCKS, len(symbols)), len(symbols)))
    bases = {}
    for found in run_tasks(store, {"as_of": as_of, "t": t, "market": market}, _batch_bases, batches,
                           workers=workers if len(symbols) >= PARALLEL_MIN_STOCKS else 1, progress=report):
        bases.update(found)
    return bases


def _batch_bases(state: dict, symbols: list[str]) -> dict[str, dict]:
    return {symbol: stock_setup(state["store"], symbol, state["as_of"], state["t"], state["market"])[1]
            for symbol in symbols}


def buyable(base: dict, day: str, t: Thresholds) -> bool:
    """A valid breakout on this close, or within buy_zone_entry_weeks of it, still in the buy zone."""
    breakout = base.get("breakout_date")
    if breakout is None or breakout > day or base.get("position_pass") is not True or base.get("volume_pass") is not True:
        return False
    return (date.fromisoformat(day) - date.fromisoformat(breakout)).days <= 7 * t.buy_zone_entry_weeks


def technical_criteria(base: dict, market: dict, t: Thresholds, as_of: str) -> list[Criterion]:
    current = market["as_of"] == as_of
    state = market["state"] if current else None
    # Older callers pass only a state; the specification's gate is then 100% or 0%.
    exposure = market.get("exposure", 100.0 if state == CONFIRMED else 0.0) if current and state else None
    return [
        Criterion("n_position", "N", "Price above base pivot", base["distance_pct"], base["position_pass"],
                  f"From pivot to {t.buy_zone_max_pct:g}% above", base["reason"]),
        Criterion("n_volume", "N", "Breakout volume", base["breakout_volume_pct"], base["volume_pass"],
                  f">= {t.breakout_volume_pct:g}% above prior {t.breakout_volume_sessions}-session average",
                  f"First closing breakout: {base['breakout_date']}." if base["breakout_date"] else base["reason"]),
        Criterion("m_direction", "M", "Market direction", state,
                  exposure > 0 if exposure is not None else None, "Market allows new buying (exposure above 0%)",
                  (f"Allowed exposure {exposure:g}%. " if exposure is not None else "")
                  + INDEX_RULES[t.market_index_rule] + " The deciding index needs current, complete evidence."),
    ]
