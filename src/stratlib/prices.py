"""Daily price bars and the rules for keeping them current.

Pure functions only; fetching lives in backfill.py.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .store import PriceState

ET = ZoneInfo("America/New_York")

# Statuses that mean "checked, nothing more to get until new data is out".
# An "error" status is retried on the next run.
SETTLED_STATUSES = frozenset({"ok", "empty", "restricted"})


@dataclass(frozen=True)
class Bar:
    date: str  # ISO date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_bars(rows: Iterable[dict]) -> list[Bar]:
    """FMP EOD rows (newest first) to bars sorted oldest first.

    Rows without a date or close are dropped; a repeated date keeps the last row.
    """
    bars: dict[str, Bar] = {}
    for row in rows:
        day = row.get("date")
        close = _num(row.get("close", row.get("price")))
        if not day or close is None:
            continue
        day = str(day)[:10]
        bars[day] = Bar(
            date=day,
            open=_num(row.get("open")),
            high=_num(row.get("high")),
            low=_num(row.get("low")),
            close=close,
            volume=_num(row.get("volume")),
        )
    return [bars[d] for d in sorted(bars)]


def eod_cutoff(now: datetime, final_hour_et: int) -> date:
    """Latest date whose daily bar is final.

    FMP serves an in-progress bar for the current session, so today's bar only
    counts once the clock in New York has passed ``final_hour_et``.
    """
    local = now.astimezone(ET)
    return local.date() if local.hour >= final_hour_et else local.date() - timedelta(days=1)


def last_eod_release(now: datetime, final_hour_et: int) -> datetime:
    """The most recent weekday moment when a new daily bar became final.

    A symbol checked after this moment has nothing new to fetch.
    """
    local = now.astimezone(ET)
    release = local.replace(hour=final_hour_et, minute=0, second=0, microsecond=0)
    if release > local:
        release -= timedelta(days=1)
    while release.weekday() >= 5:
        release -= timedelta(days=1)
    return release


def history_start(cutoff: date, years: int) -> date:
    try:
        return cutoff.replace(year=cutoff.year - years)
    except ValueError:  # 29 February
        return cutoff.replace(year=cutoff.year - years, day=28)


def is_restated(stored_close: float | None, fetched_close: float, tolerance_pct: float) -> bool:
    """True when the same day's close moved more than the tolerance, which
    means FMP re-adjusted the history (a split or a correction)."""
    if not stored_close:
        return True
    return abs(fetched_close - stored_close) / abs(stored_close) * 100 > tolerance_pct


@dataclass(frozen=True)
class PriceJob:
    symbol: str
    start: date
    end: date
    # Full jobs download [requested_from, end] and replace stored history.
    full: bool
    requested_from: date
    # Incremental jobs re-fetch the last stored bar to detect restatements.
    overlap: tuple[str, float | None] | None = None


def plan_price_job(
    symbol: str,
    state: PriceState | None,
    *,
    desired_start: date,
    cutoff: date,
    release: datetime,
) -> PriceJob | None:
    """Decide what to fetch for one symbol, or None if it is already current."""
    full = PriceJob(symbol, desired_start, cutoff, True, desired_start)
    if state is None:
        return full
    # No recorded start means no successful fetch yet (restricted or failed).
    requested_from = (
        date.fromisoformat(state.requested_from) if state.requested_from else desired_start
    )
    if desired_start < requested_from:
        # history_years was raised; download the longer history.
        return full
    settled = (
        state.checked_at is not None
        and state.checked_at >= release
        and state.status in SETTLED_STATUSES
    )
    if settled:
        return None
    if state.last_date is None:
        return PriceJob(symbol, requested_from, cutoff, True, requested_from)
    last = date.fromisoformat(state.last_date)
    if last >= cutoff:
        return None
    return PriceJob(
        symbol, last, cutoff, False, requested_from, overlap=(state.last_date, state.last_close)
    )
