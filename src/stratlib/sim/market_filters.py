"""Market filters, each a boolean per session (known at that session's close).

A: none. B: SPY close above its 50-day SMA. C: QQQ 10-day SMA above its 20-day SMA.
D: more than half of the day's universe closes above its 50-day SMA.
E: new 52-week highs minus new 52-week lows across the universe, 10-day average, above zero.
F: B and D.

"Universe" for D and E: stocks passing the ground rules ($5, $20M) that day. ETFs are left out:
inverse ETFs rise when the market falls, and hundreds of index ETFs would dominate the count.
A new 52-week high is a high above the highest high of the previous 251 sessions (lows likewise).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LABELS = {
    "A": "No filter",
    "B": "SPY above its 50-day SMA",
    "C": "QQQ 10-day SMA above its 20-day SMA",
    "D": "Over 50% of stocks above their 50-day SMA",
    "E": "New highs minus new lows, 10-day average, above zero",
    "F": "B and D",
}


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.DataFrame(x).rolling(n, min_periods=n).mean().to_numpy()


def compute(panel) -> dict:
    p = panel
    spy, qqq = p.index["SPY"], p.index["QQQ"]
    c = p.close_ff
    spy_sma50 = _sma(c[:, [spy]], 50)[:, 0]
    qqq_10, qqq_20 = _sma(c[:, [qqq]], 10)[:, 0], _sma(c[:, [qqq]], 20)[:, 0]
    stocks = np.flatnonzero(p.kind == "stock")
    close, high, low = p.close[:, stocks], p.high[:, stocks], p.low[:, stocks]
    eligible = p.eligible[:, stocks]
    with np.errstate(invalid="ignore"):
        sma50 = _sma(close, 50)
        counted = eligible & np.isfinite(sma50)
        breadth = (counted & (close > sma50)).sum(axis=1) / np.maximum(counted.sum(axis=1), 1)
        prior_high = np.full_like(high, np.nan)
        prior_low = np.full_like(low, np.nan)
        prior_high[1:] = pd.DataFrame(high).rolling(251, min_periods=251).max().to_numpy()[:-1]
        prior_low[1:] = pd.DataFrame(low).rolling(251, min_periods=251).min().to_numpy()[:-1]
        new_highs = (eligible & (high > prior_high)).sum(axis=1)
        new_lows = (eligible & (low < prior_low)).sum(axis=1)
        net = pd.Series(new_highs - new_lows, dtype=float).rolling(10, min_periods=10).mean().to_numpy()
        masks = {
            "A": np.ones(len(p.dates), dtype=bool),
            "B": c[:, spy] > spy_sma50,
            "C": qqq_10 > qqq_20,
            "D": breadth > 0.5,
            "E": net > 0,
        }
    masks["F"] = masks["B"] & masks["D"]
    return {"masks": masks, "breadth": breadth, "net_highs_lows": net, "new_highs": new_highs, "new_lows": new_lows}


class Filtered:
    """Wraps a strategy so that it signals only on sessions where the filter is on.

    Everything else is delegated to the wrapped strategy, which is set up once and shared, so
    a filter changes nothing but which signal days count.
    """

    def __init__(self, inner, mask: np.ndarray, code: str, shared: dict):
        self.inner, self.mask, self.code, self.shared = inner, mask, code, shared
        self.name = f"{inner.name}+filter_{code}"

    def setup(self, panel, params):
        key = id(self.inner)
        if self.shared.get(key) is None:
            self.inner.setup(panel, {k: v for k, v in params.items() if k != "market_filter"})
            self.shared[key] = True
        self.panel, self.params = panel, self.inner.params

    def candidates(self, t):
        return self.inner.candidates(t) if self.mask[t] else np.array([], dtype=int)

    def orders(self, t):
        return self.inner.orders(t) if self.mask[t] else {}

    def __getattr__(self, attr):
        return getattr(self.inner, attr)
