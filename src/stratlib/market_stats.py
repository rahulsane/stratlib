"""Pure price filters and cross-sectional ranks. No I/O or GUI dependencies."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from operator import ge, gt, le
from statistics import mean

from .config import Thresholds
from .prices import Bar


@dataclass(frozen=True)
class PriceMetrics:
    close: float | None = None
    high_52w: float | None = None
    below_high_pct: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    avg_volume: float | None = None
    rs_score: float | None = None
    industry_return: float | None = None
    price_pass: bool = False
    reason: str = "No price history"


@dataclass(frozen=True)
class PriceCheck:
    key: str
    label: str
    value: float | None
    threshold: float | None
    comparison: str
    passed: bool | None
    failure: str


def price_filter_checks(m: PriceMetrics, t: Thresholds) -> list[PriceCheck]:
    """One source for the prefilter's decisions and their stock-detail display."""
    checks = [
        ("price", "Close", m.close, t.min_price, ">=", ge, "Price below minimum"),
        ("volume", f"Average volume over {t.volume_sessions} sessions", m.avg_volume, t.min_avg_volume,
         ">=", ge, "Average volume below minimum or missing"),
        ("high", f"Distance below {t.high_sessions}-session high", m.below_high_pct, t.max_below_high_pct,
         "<=", le, "Outside high proximity or missing history"),
        ("ma50", f"Close above {t.short_ma_sessions}-session average", m.close, m.ma50,
         ">", gt, "Not above short moving average"),
        ("ma200", f"Close above {t.long_ma_sessions}-session average", m.close, m.ma200,
         ">", gt, "Not above long moving average"),
    ]
    return [PriceCheck(key, label, value, threshold, comparison,
                       compare(value, threshold) if value is not None and threshold is not None else None, failure)
            for key, label, value, threshold, comparison, compare, failure in checks]


def price_metrics(bars: list[Bar], sessions: list[str], t: Thresholds) -> PriceMetrics:
    """Use the same exchange sessions for every stock, including RS anchors.

    No short-history annualisation. A stock needs 253 closes for 252-session
    RS. Missing/stale sessions produce an unavailable metric, never zero.
    """
    if not bars or not sessions:
        return PriceMetrics()
    by_day = {b.date: b for b in bars}
    if sessions[-1] not in by_day:
        return PriceMetrics(reason=f"No bar for {sessions[-1]}")

    def window(n):
        if len(sessions) < n:
            return []
        selected = [by_day.get(d) for d in sessions[-n:]]
        if any(b is None or not math.isfinite(b.close) or b.close <= 0 for b in selected):
            return []
        return selected

    latest = by_day[sessions[-1]]
    if not math.isfinite(latest.close) or latest.close <= 0:
        return PriceMetrics(reason="Invalid latest close")
    short, long = window(t.short_ma_sessions), window(t.long_ma_sessions)
    highs, volumes = window(t.high_sessions), window(t.volume_sessions)
    ma50 = mean(b.close for b in short) if short else None
    ma200 = mean(b.close for b in long) if long else None
    high = max(b.high for b in highs) if highs and all(
        b.high is not None and math.isfinite(b.high) and b.high > 0 for b in highs
    ) else None
    avg_volume = mean(b.volume for b in volumes) if volumes and all(
        b.volume is not None and math.isfinite(b.volume) and b.volume >= 0 for b in volumes
    ) else None
    below = 100 * (1 - latest.close / high) if high else None
    rs_bars = window(4 * t.rs_quarter_sessions + 1)
    score = None
    if rs_bars:
        closes = [rs_bars[-1 - i * t.rs_quarter_sessions].close for i in range(5)]
        quarters = [closes[i] / closes[i + 1] - 1 for i in range(4)]
        score = 100 * (t.rs_recent_weight * quarters[0] + sum(quarters[1:])) / (t.rs_recent_weight + 3)
    industry_bars = window(t.industry_sessions + 1)
    industry_return = 100 * (industry_bars[-1].close / industry_bars[0].close - 1) if industry_bars else None
    metrics = PriceMetrics(latest.close, high, below, ma50, ma200, avg_volume, score, industry_return)
    failures = [c.failure for c in price_filter_checks(metrics, t) if c.passed is not True]
    return replace(metrics, price_pass=not failures, reason="; ".join(failures) or "Passed")


def rs_ratings(scores: dict[str, float | None]) -> dict[str, int]:
    """1..99 using average tied rank, linear endpoints, round half up.

    A single observation, or a wholly tied population, ranks 50.
    The caller supplies the full universe before any price or sample filter.
    """
    ordered = sorted((score, symbol) for symbol, score in scores.items()
                     if score is not None and math.isfinite(score))
    n = len(ordered)
    result = {}
    i = 0
    while i < n:
        j = i + 1
        while j < n and ordered[j][0] == ordered[i][0]:
            j += 1
        rank = 50 if n == 1 else math.floor(1 + 98 * ((i + j - 1) / 2) / (n - 1) + 0.5)
        for _, symbol in ordered[i:j]:
            result[symbol] = rank
        i = j
    return result


def industry_ranks(industries: dict[str, str | None], returns: dict[str, float | None]) -> dict[str, dict]:
    """Equal-weight member returns, competition ranks; ties share a rank."""
    groups = defaultdict(list)
    for symbol, industry in industries.items():
        value = returns.get(symbol)
        if industry and value is not None and math.isfinite(value):
            groups[industry].append(value)
    ordered = sorted(((mean(values), name) for name, values in groups.items()), reverse=True)
    result = {}
    last = None
    rank = 0
    for i, (value, name) in enumerate(ordered, 1):
        if value != last:
            rank = i
        result[name] = {"rank": rank, "return_pct": value, "members": len(groups[name])}
        last = value
    return result
