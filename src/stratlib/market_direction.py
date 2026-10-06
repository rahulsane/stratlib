"""Sequential, prefix-stable market direction rules. No storage or network I/O."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .config import Thresholds
from .prices import Bar

CONFIRMED = "confirmed uptrend"
PRESSURE = "uptrend under pressure"
CORRECTION = "correction"
INDEXES = {"^GSPC": ("S&P 500", "SPY"), "^IXIC": ("Nasdaq Composite", "QQQ")}
# How the two index states combine into M. "both" is the specification's
# reading: each index must confirm, so the weaker one decides.
INDEX_RULES = {
    "both": "The weaker index determines M: both must be in a confirmed uptrend.",
    "either": "The stronger index determines M: one confirmed index is enough.",
    "sp500": "The S&P 500 alone determines M; the Nasdaq Composite is shown for context.",
    "nasdaq": "The Nasdaq Composite alone determines M; the S&P 500 is shown for context.",
    "average": "Both indexes count equally: M allows the average of their exposures, rounded down to 20% steps.",
    "ignore": "Market direction is ignored: 100% of the portfolio may always be invested. For research only.",
}
RANK = {CORRECTION: 0, PRESSURE: 1, CONFIRMED: 2}
EXPOSURE_STEP = 20


def positive(value) -> bool:
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class MarketDay:
    date: str
    state: str | None
    distribution_count: int
    distribution: bool | None
    follow_through: bool
    last_follow_through: str | None
    rally_day: int | None
    rally_low: float | None
    volume_source: str | None
    volume: float | None
    change_pct: float | None
    reason: str
    exposure: float | None = None       # share of the portfolio this index allows invested
    drawdown_pct: float | None = None   # close below the uptrend's peak close


def exposure_for(state: str | None, count: int, sessions_since_ftd: int | None, t: Thresholds) -> float | None:
    """The exposure ladder. Defaults allow 100% in a confirmed uptrend and nothing otherwise."""
    if state is None:
        return None
    if state == CORRECTION:
        return 0.0
    if state == PRESSURE:
        return t.exposure_heavy_pressure_pct if count >= t.distribution_heavy_count else t.exposure_pressure_pct
    value = t.exposure_late_confirmed_pct if count >= t.distribution_pressure_count - 1 else t.exposure_confirmed_pct
    if sessions_since_ftd is not None and sessions_since_ftd < t.exposure_new_uptrend_sessions:
        value = min(value, t.exposure_new_uptrend_pct)
    return value


def index_direction(bars: list[Bar], proxy: list[Bar], symbol: str, proxy_symbol: str,
                    t: Thresholds, *, sessions: list[str] | None = None,
                    as_of: str = "9999-12-31") -> list[MarketDay]:
    """Start in correction; a follow-through is required to confirm an uptrend.

    A rally is counted from its intraday low. Undercutting it resets the count.
    With correction_drawdown_pct above zero, distribution days only reach
    "under pressure": a correction needs a close that far below the uptrend's
    peak close, or an undercut of the rally low.
    With distribution_expiry_gain_pct above zero, a distribution day stops
    counting once a later close is that far above its close (IBD's 5% rule).
    Expiry depends only on closes through the current session.
    Index volume is used unless either comparison bar is invalid or its most
    recent volume window is constant. A fallback compares BOTH ETF volumes.
    Missing sessions/volume invalidate M until a complete distribution window
    exists again. Nothing later than a given session affects that session.
    """
    prices = {b.date: b for b in bars if b.date <= as_of}
    proxies = {b.date: b for b in proxy if b.date <= as_of}
    dates = sorted(set(d for d in sessions if d <= as_of) if sessions is not None
                   else prices.keys() | proxies.keys())
    result = []
    state, low, low_index, last_ftd = CORRECTION, None, None, None
    ftd_index = peak = None
    drawdown_rule = t.correction_drawdown_pct > 0
    distributions: list[bool | None] = []
    active: list[bool] = []            # a distribution day not yet expired by a rally
    closes: list[float | None] = []
    index_volumes = []
    for i, day in enumerate(dates):
        current = prices.get(day)
        previous = prices.get(dates[i - 1]) if i else None
        index_volumes.append(current.volume if current else None)
        pair_ok = current is not None and previous is not None and all(
            positive(v) for v in (current.close, current.low, previous.close, previous.low))
        source = None
        volume = None
        prev_volume = None
        recent = index_volumes[-t.market_volume_stale_sessions:]
        constant = len(recent) == t.market_volume_stale_sessions and len(set(recent)) == 1
        if pair_ok and positive(current.volume) and positive(previous.volume) and not constant:
            source, volume, prev_volume = symbol, current.volume, previous.volume
        elif pair_ok:
            p, prev_p = proxies.get(day), proxies.get(dates[i - 1])
            if p and prev_p and positive(p.volume) and positive(prev_p.volume):
                source, volume, prev_volume = proxy_symbol, p.volume, prev_p.volume
        change = 100 * (current.close / previous.close - 1) if pair_ok else None
        dist = (change <= -t.distribution_decline_pct + 1e-9 and volume > prev_volume
                if source else None)
        distributions.append(dist)
        active.append(dist is True)
        closes.append(current.close if current else None)
        first = max(0, i + 1 - t.distribution_window_sessions)
        expired = 0
        if t.distribution_expiry_gain_pct > 0 and current and positive(current.close):
            for j in range(first, i):
                if active[j] and current.close >= closes[j] * (1 + t.distribution_expiry_gain_pct / 100) - 1e-9:
                    active[j] = False
            expired = sum(d is True and not a for d, a in zip(distributions[first:], active[first:]))
        window = distributions[-t.distribution_window_sessions:]
        count = sum(active[first:])
        ready = len(window) == t.distribution_window_sessions and all(d is not None for d in window)
        ftd = False
        reason = "Waiting for a follow-through day."
        if not source:
            state, low, low_index = CORRECTION, None, None
            reason = "Missing index prices or usable volume. Run the market-symbol backfill."
        else:
            breached = low is not None and current.low < low
            if low is None or breached:
                low, low_index, state = current.low, i, CORRECTION
            rally_day = i - low_index + 1
            if not drawdown_rule and count >= t.distribution_correction_count:
                if state != CORRECTION:
                    low, low_index = current.low, i
                state = CORRECTION
                reason = f"{count} distribution days ended the uptrend."
            elif state == CORRECTION:
                if (not breached and ready and rally_day >= t.follow_through_min_day
                        and change + 1e-9 >= t.follow_through_gain_pct and volume > prev_volume):
                    ftd, last_ftd, ftd_index, peak = True, day, i, current.close
                    state = PRESSURE if count >= t.distribution_pressure_count else CONFIRMED
                    reason = f"Follow-through on rally day {rally_day}."
                elif breached:
                    reason = "Rally low undercut; a new rally count starts here."
            else:
                peak = max(peak or current.close, current.close)
                fall = 100 * (1 - current.close / peak)
                if drawdown_rule and fall + 1e-9 >= t.correction_drawdown_pct:
                    state, low, low_index = CORRECTION, current.low, i
                    reason = f"Closed {fall:.1f}% below the uptrend's peak; a new rally count starts here."
                else:
                    state = PRESSURE if count >= t.distribution_pressure_count else CONFIRMED
                    reason = f"{count} distribution days in the last {t.distribution_window_sessions} sessions."
        if expired:
            reason += f" {expired} more expired after a {t.distribution_expiry_gain_pct:g}% rally."
        if current and positive(current.low) and low is None:
            low, low_index = current.low, i
        if not ready:
            reason = f"Need {t.distribution_window_sessions} consecutive price/volume comparisons. " + reason
        shown = state if ready else None
        uptrend = shown in (CONFIRMED, PRESSURE) and ftd_index is not None
        result.append(MarketDay(day, shown, count, dist, ftd, last_ftd,
                                i - low_index + 1 if low_index is not None else None,
                                low, source, volume, change, reason,
                                exposure_for(shown, count, i - ftd_index if uptrend else None, t),
                                100 * (1 - current.close / peak) if uptrend and current and peak else None))
    return result


def market_direction(histories: dict[str, list[Bar]], t: Thresholds,
                     *, as_of: str = "9999-12-31") -> dict:
    """Combine the index states by t.market_index_rule. Missing evidence is unavailable, not a fourth state."""
    sessions = sorted({b.date for bars in histories.values() for b in bars if b.date <= as_of})
    indexes = {}
    for symbol, (name, proxy) in INDEXES.items():
        days = index_direction(histories.get(symbol, []), histories.get(proxy, []), symbol,
                               proxy, t, sessions=sessions, as_of=as_of)
        indexes[symbol] = {"name": name, "proxy": proxy,
                           "latest": asdict(days[-1]) if days else None,
                           "history": [asdict(d) for d in days]}
    rule = t.market_index_rule
    deciding = {"sp500": ["^GSPC"], "nasdaq": ["^IXIC"]}.get(rule, list(INDEXES))
    combined = []
    for i, day in enumerate(sessions):
        rows = [indexes[symbol]["history"][i] for symbol in deciding]
        states, exposures = [r["state"] for r in rows], [r["exposure"] for r in rows]
        if None in states:
            state = exposure = None
        elif rule == "ignore":
            state, exposure = min(states, key=RANK.get), 100.0
        elif rule == "average":
            exposure = EXPOSURE_STEP * math.floor(sum(exposures) / len(exposures) / EXPOSURE_STEP + 1e-9)
            state = (CORRECTION if exposure <= 0 else CONFIRMED if exposure >= t.exposure_late_confirmed_pct
                     else PRESSURE)
        else:
            pick = max if rule == "either" else min
            state, exposure = pick(states, key=RANK.get), pick(exposures)
        combined.append({"date": day, "state": state, "exposure": exposure})
    warnings = []
    for symbol, entry in indexes.items():
        recent = entry["history"][-t.distribution_window_sessions:]
        proxy_days = sum(d["volume_source"] == entry["proxy"] for d in recent)
        if proxy_days:
            warnings.append(f"{entry['name']} uses {entry['proxy']} volume on {proxy_days} of the last "
                            f"{len(recent)} sessions because index volume was missing, nonpositive or constant.")
        if not entry["latest"] or entry["latest"]["state"] is None:
            warnings.append(f"{entry['name']}: insufficient continuous price/volume data. Run stratlib backfill "
                            f"--symbols {symbol},{entry['proxy']}.")
    return {"as_of": sessions[-1] if sessions else None,
            "state": combined[-1]["state"] if combined else None,
            "exposure": combined[-1]["exposure"] if combined else None,
            "indexes": indexes, "history": combined, "warnings": warnings}
