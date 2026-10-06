"""Deterministic weekly bases and breakout measurements, with no I/O."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from statistics import mean

from .config import Thresholds
from .prices import Bar


def _positive(value):
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class Week:
    start: str
    end: str                    # last actual session, for availability and overlays
    week_end: str               # calendar Friday, for duration and gap checks
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    sessions: int


@dataclass(frozen=True)
class Base:
    pattern: str
    start: str
    end: str
    length_weeks: int
    depth_pct: float
    high: float
    low: float
    pivot: float
    handle_start: str | None = None
    handle_depth_pct: float | None = None
    handle_volume_ratio: float | None = None
    correction_during_base: bool = False


def history_weeks(t: Thresholds) -> int:
    """Weeks of daily bars detect_base reads, including a second-stage flat base's earlier base."""
    extra = t.flat_prior_breakout_weeks + t.base_max_weeks if t.flat_prior_breakout_weeks else 0
    return t.base_lookback_weeks + extra + 1


def weekly_bars(bars: list[Bar], as_of: str, *, sessions: list[str] | None = None) -> list[Week]:
    """Completed Friday-ended weeks only; reject missing expected stock sessions.

    A holiday-shortened week is complete once its calendar Friday has elapsed.
    Volume is unavailable when even one daily measurement is absent.
    """
    grouped = defaultdict(dict)
    for b in bars:
        if b.date > as_of:
            continue
        day = date.fromisoformat(b.date)
        if day.weekday() > 4:
            continue
        friday = (day + timedelta(days=4 - day.weekday())).isoformat()
        grouped[friday][b.date] = b
    expected = defaultdict(set)
    if sessions is not None:
        for day in sessions:
            d = date.fromisoformat(day)
            if day <= as_of and d.weekday() < 5:
                expected[(d + timedelta(days=4 - d.weekday())).isoformat()].add(day)
    result = []
    for friday, days in sorted(grouped.items()):
        if friday > as_of or (sessions is not None and set(days) != expected[friday]):
            continue
        week = [days[d] for d in sorted(days)]
        if not all(all(_positive(v) for v in (b.open, b.high, b.low, b.close))
                   and b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in week):
            continue
        volume = (sum(b.volume for b in week) if all(
            b.volume is not None and math.isfinite(b.volume) and b.volume >= 0 for b in week) else None)
        result.append(Week(week[0].date, week[-1].date, friday, week[0].open,
                           max(b.high for b in week), min(b.low for b in week),
                           week[-1].close, volume, len(week)))
    return result


def _continuous(weeks: list[Week]) -> bool:
    return all((date.fromisoformat(b.week_end) - date.fromisoformat(a.week_end)).days == 7
               for a, b in zip(weeks, weeks[1:]))


def _base(pattern, weeks, pivot, **extra):
    high, low = max(w.high for w in weeks), min(w.low for w in weeks)
    return Base(pattern, weeks[0].start, weeks[-1].end, len(weeks),
                100 * (1 - low / high), high, low, pivot, **extra)


def _cup_with_handle(weeks: list[Week], t: Thresholds, correction_dates: frozenset[str]) -> Base | None:
    for n in range(t.handle_min_weeks, t.handle_max_weeks + 1):
        if len(weeks) < t.cup_min_weeks + n:
            continue
        cup, handle = weeks[:-n], weeks[-n:]
        high, low = cup[0].high, min(w.low for w in cup)
        trough = min(range(len(cup)), key=lambda i: cup[i].low)
        # Distinct left rim, interior trough and recovered right rim.
        if not t.cup_side_min_weeks <= trough <= len(cup) - t.cup_side_min_weeks - 1:
            continue
        if any(w.high > high for w in cup) or cup[-1].close < high * (1 - t.base_rim_tolerance_pct / 100):
            continue
        correction = any(weeks[0].start <= d <= weeks[-1].end for d in correction_dates)
        maximum = t.cup_correction_max_depth_pct if correction else t.cup_max_depth_pct
        depth = 100 * (1 - low / high)
        if not t.cup_min_depth_pct - 1e-9 <= depth <= maximum + 1e-9:
            continue
        hh, hl = max(w.high for w in handle), min(w.low for w in handle)
        hd = 100 * (1 - hl / hh)
        if (hh > high or hl < (high + low) / 2 or hd > t.handle_max_depth_pct + 1e-9
                or handle[-1].close >= cup[-1].close or handle[-1].close >= handle[0].open):
            continue
        if not all(_positive(w.volume) for w in weeks):
            continue
        cup_volume = sum(w.volume for w in cup) / sum(w.sessions for w in cup)
        ratio = (sum(w.volume for w in handle) / sum(w.sessions for w in handle)) / cup_volume
        if ratio >= t.handle_volume_max_ratio:
            continue
        return _base("Cup with handle", weeks, hh + t.cup_pivot_offset,
                     handle_start=handle[0].start, handle_depth_pct=hd,
                     handle_volume_ratio=ratio, correction_during_base=correction)
    return None


def _cup_without_handle(weeks: list[Week], t: Thresholds, correction_dates: frozenset[str]) -> Base | None:
    """A cup whose right side recovers to within the rim tolerance, with no handle yet.

    The pivot is the left rim's high plus the cup offset, as in O'Neil's
    cup without handle. The depth and side rules match the cup with handle.
    """
    if not t.cup_without_handle_min_weeks or len(weeks) < t.cup_without_handle_min_weeks:
        return None
    high, low = weeks[0].high, min(w.low for w in weeks)
    trough = min(range(len(weeks)), key=lambda i: weeks[i].low)
    if not t.cup_side_min_weeks <= trough <= len(weeks) - t.cup_side_min_weeks - 1:
        return None
    if any(w.high > high for w in weeks) or weeks[-1].close < high * (1 - t.base_rim_tolerance_pct / 100):
        return None
    correction = any(weeks[0].start <= d <= weeks[-1].end for d in correction_dates)
    maximum = t.cup_correction_max_depth_pct if correction else t.cup_max_depth_pct
    if not t.cup_min_depth_pct - 1e-9 <= 100 * (1 - low / high) <= maximum + 1e-9:
        return None
    return _base("Cup without handle", weeks, high + t.cup_pivot_offset, correction_during_base=correction)


def _double_bottom(weeks: list[Week], t: Thresholds) -> Base | None:
    if len(weeks) < t.double_bottom_min_weeks:
        return None
    troughs = [i for i in range(1, len(weeks) - 1)
               if weeks[i].low < weeks[i - 1].low and weeks[i].low <= weeks[i + 1].low]
    for first, second in zip(troughs, troughs[1:]):
        if second - first < 2 or weeks[second].low >= weeks[first].low:
            continue
        peak = max(w.high for w in weeks[first + 1:second])
        # W geometry: middle peak rises above both trough bars; right side recovers.
        if (peak <= max(weeks[first].high, weeks[second].high) or peak > weeks[0].high
                or weeks[-1].close < peak * (1 - t.base_rim_tolerance_pct / 100)
                or any(w.close > peak for w in weeks[second:])
                or min(w.low for w in weeks) != weeks[second].low):
            continue
        return _base("Double bottom", weeks, peak)
    return None


def _flat_base(weeks: list[Week], t: Thresholds) -> Base | None:
    if len(weeks) < t.flat_min_weeks:
        return None
    high_i = max(range(len(weeks)), key=lambda i: weeks[i].high)
    low_i = min(range(len(weeks)), key=lambda i: weeks[i].low)
    base = _base("Flat base", weeks, weeks[high_i].high)
    # A correction must follow a high; a steady advance is not a flat base.
    return base if high_i < low_i and base.depth_pct <= t.flat_max_depth_pct + 1e-9 else None


def _prior_breakout(weeks: list[Week], flat_index: int, flat_high: float, daily: list[Bar], t: Thresholds,
                    correction_dates: frozenset[str], found: dict) -> dict | None:
    """An earlier base that ended within flat_prior_breakout_weeks of the flat base, closed
    above its pivot before the flat base began, and from whose pivot the stock then rose
    at least flat_prior_advance_pct to the flat base's high.

    Any pattern counts; the earlier base need not itself be second-stage. ``found``
    memoises bases by (start, end) within one detect_base call.
    """
    flat_start = weeks[flat_index].start
    horizon = (date.fromisoformat(flat_start) - timedelta(weeks=t.flat_prior_breakout_weeks)).isoformat()
    ceiling = flat_high / (1 + t.flat_prior_advance_pct / 100) + 1e-9
    detectors = [(_flat_base, t.flat_min_weeks), (_cup_with_handle, t.cup_min_weeks + t.handle_min_weeks),
                 (_double_bottom, t.double_bottom_min_weeks)]
    if t.cup_without_handle_min_weeks:
        detectors.append((_cup_without_handle, t.cup_without_handle_min_weeks))
    for end in range(flat_index, 0, -1):
        if weeks[end - 1].week_end < horizon:
            break
        earliest = max(0, end - t.base_max_weeks)
        for i in range(end - 1, earliest, -1):
            if not _continuous(weeks[i - 1:i + 1]):
                earliest = i
                break
        for detector, minimum in detectors:
            for start in range(earliest, end - minimum + 1):
                key = (detector.__name__, start, end)
                if key not in found:
                    window = weeks[start:end]
                    found[key] = (detector(window, t, correction_dates)
                                  if detector in (_cup_with_handle, _cup_without_handle) else detector(window, t))
                base = found[key]
                if base is None or base.pivot > ceiling:
                    continue
                breakout = next((b for b in daily if base.end < b.date < flat_start and b.close >= base.pivot), None)
                if breakout:
                    return {"pattern": base.pattern, "start": base.start, "end": base.end, "pivot": base.pivot,
                            "breakout_date": breakout.date,
                            "advance_pct": 100 * (flat_high / base.pivot - 1)}
    return None


def cup_with_handle(weeks: list[Week], t: Thresholds,
                    correction_dates: frozenset[str] = frozenset()) -> Base | None:
    return _cup_with_handle(weeks, t, correction_dates) if _continuous(weeks) else None


def cup_without_handle(weeks: list[Week], t: Thresholds,
                       correction_dates: frozenset[str] = frozenset()) -> Base | None:
    return _cup_without_handle(weeks, t, correction_dates) if _continuous(weeks) else None


def double_bottom(weeks: list[Week], t: Thresholds) -> Base | None:
    return _double_bottom(weeks, t) if _continuous(weeks) else None


def flat_base(weeks: list[Week], t: Thresholds) -> Base | None:
    return _flat_base(weeks, t) if _continuous(weeks) else None


def detect_base(bars: list[Bar], as_of: str, t: Thresholds, *,
                correction_dates: frozenset[str] = frozenset(),
                sessions: list[str] | None = None) -> dict:
    """Prefer the most recent base end, then cup with handle, cup without handle
    (when enabled), double bottom, flat, then longest.

    Completed bases from the last three weeks are retained only if a later
    daily close reached the pivot. A subsequent low below the base invalidates
    it. The first closing breakout fixes its volume measurement permanently.
    """
    daily = sorted({b.date: b for b in bars if b.date <= as_of}.values(), key=lambda b: b.date)
    empty = {"pattern": None, "start": None, "end": None, "length_weeks": None,
             "depth_pct": None, "pivot": None, "distance_pct": None, "prior_base": None,
             "breakout_date": None, "breakout_volume_pct": None,
             "position_pass": False, "volume_pass": False, "available": True,
             "reason": "No valid completed weekly base found."}
    if not daily or not _positive(daily[-1].close) or daily[-1].date != as_of:
        return {**empty, "available": False, "position_pass": None, "volume_pass": None,
                "reason": f"No valid closing price on {as_of}."}
    # Second-stage flat bases look further back, for the earlier base and its breakout.
    since = (date.fromisoformat(as_of) - timedelta(weeks=history_weeks(t))).isoformat()
    recent = [b for b in daily if b.date >= since]
    recent_sessions = [d for d in sessions if d >= since] if sessions is not None else None
    history = weekly_bars(recent, as_of, sessions=recent_sessions)
    weeks = history[-t.base_lookback_weeks:]
    offset, prior_found = len(history) - len(weeks), {}
    if len(weeks) < min(t.flat_min_weeks, t.double_bottom_min_weeks, t.cup_min_weeks + t.handle_min_weeks):
        return {**empty, "available": False, "position_pass": None, "volume_pass": None,
                "reason": "Insufficient complete weekly OHLC history."}
    for end in range(len(weeks), max(0, len(weeks) - t.base_recent_weeks - 1), -1):
        # Validate the contiguous suffix once, rather than inside every candidate.
        earliest = max(0, end - t.base_max_weeks)
        for i in range(end - 1, earliest, -1):
            if not _continuous(weeks[i - 1:i + 1]):
                earliest = i
                break
        detectors = [(_cup_with_handle, t.cup_min_weeks + t.handle_min_weeks)]
        if t.cup_without_handle_min_weeks:
            detectors.append((_cup_without_handle, t.cup_without_handle_min_weeks))
        detectors += [(_double_bottom, t.double_bottom_min_weeks), (_flat_base, t.flat_min_weeks)]
        for detector, minimum in detectors:
            for start in range(earliest, end - minimum + 1):
                selected = weeks[start:end]
                base = (detector(selected, t, correction_dates) if detector in (_cup_with_handle, _cup_without_handle)
                        else detector(selected, t))
                if base is None:
                    continue
                if sessions is not None and {b.date for b in daily if b.date >= base.start} != {
                    d for d in sessions if base.start <= d <= as_of
                }:
                    continue
                after = [b for b in daily if b.date > base.end]
                if any(not _positive(b.low) or b.low < base.low for b in after):
                    continue
                breakout = next((b for b in after if b.close >= base.pivot), None)
                if end != len(weeks) and breakout is None:
                    continue
                # Do not resurrect an old base when recent complete weeks are absent.
                if (date.fromisoformat(as_of) - date.fromisoformat(selected[-1].week_end)).days >= 7 * (t.base_recent_weeks + 1):
                    continue
                # Last, as it is by far the costliest check: most flat bases fail one of the cheap ones above.
                prior = None
                if detector is _flat_base and t.flat_prior_breakout_weeks:
                    prior = _prior_breakout(history, offset + start, base.high, daily, t, correction_dates, prior_found)
                    if prior is None:
                        continue
                distance = 100 * (daily[-1].close / base.pivot - 1)
                volume_pct = None
                if breakout:
                    before = [b for b in daily if b.date < breakout.date][-t.breakout_volume_sessions:]
                    expected = ([d for d in sessions if d < breakout.date][-t.breakout_volume_sessions:]
                                if sessions is not None else [b.date for b in before])
                    if (len(before) == t.breakout_volume_sessions and [b.date for b in before] == expected
                            and all(_positive(b.volume) for b in [*before, breakout])):
                        volume_pct = 100 * (breakout.volume / mean(b.volume for b in before) - 1)
                return {**empty, **asdict(base), "distance_pct": distance, "prior_base": prior,
                        "breakout_date": breakout.date if breakout else None,
                        "breakout_volume_pct": volume_pct,
                        "position_pass": -1e-9 <= distance <= t.buy_zone_max_pct + 1e-9,
                        "volume_pass": (volume_pct + 1e-9 >= t.breakout_volume_pct if volume_pct is not None
                                        else None if breakout else False),
                        "reason": "First closing breakout volume is compared with the prior "
                                  f"{t.breakout_volume_sessions} sessions." if breakout else "Base formed; no closing breakout yet."}
    return empty
