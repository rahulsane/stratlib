"""9/21 EMA pullback (the Traveling Trader's trend trade) on daily bars. Rules fixed before any results.

Setup at the close of session T:
1. Trend: the 9-day EMA above the 21-day EMA, the 21-day EMA above the 50-day SMA, and the close above the
   200-day SMA.
2. Trending stock: the highest close of the last 20 sessions is also the highest close of the last 126.
3. Pullback touch: the session's low at or below the 9-day EMA, the close at or above the 21-day EMA, and the
   lows of the previous three sessions above the 9-day EMA (a fresh pullback into the 9/21 zone, not chop).
Entry: buy at the close of T ("you can enter a position here").
Initial stop: 3% below the 21-day EMA at entry ("a risk-managed stop limit" just under the EMAs); not trailed.
Exit (params["exit_rule"]):
  "first"  - sell at the first close below the 21-day EMA ("once the trend breaks and we are below the EMAs,
             get out").
  "second" - sell at the second close below the 21-day EMA; the count resets after 10 sessions back above it
             (one violation allowed, his "closed and retested").
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..engine import Position, Strategy

DEFAULTS = {
    "ema_fast": 9,
    "ema_slow": 21,
    "sma_mid": 50,
    "sma_long": 200,
    "high_recent": 20,
    "high_lookback": 126,
    "fresh_sessions": 3,
    "stop_pct": 3.0,
    "exit_rule": "first",
    "reset_sessions": 10,
}


def shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def rolling(a: np.ndarray, n: int, how: str) -> np.ndarray:
    frame = pd.DataFrame(a).rolling(n, min_periods=n)
    return getattr(frame, how)().to_numpy()


def ema(a: np.ndarray, span: int) -> np.ndarray:
    return pd.DataFrame(a).ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


class EmaPullback(Strategy):
    name = "ema_pullback"
    entry = "close"

    def setup(self, panel, params):
        params = {**DEFAULTS, **params}
        super().setup(panel, params)
        c, lo = panel.close, panel.low
        with np.errstate(invalid="ignore"):
            self.ema_fast = ema(c, params["ema_fast"])
            self.ema_slow = ema(c, params["ema_slow"])
            sma_mid = rolling(c, params["sma_mid"], "mean")
            sma_long = rolling(c, params["sma_long"], "mean")
            trend = (self.ema_fast > self.ema_slow) & (self.ema_slow > sma_mid) & (c > sma_long)
            hi_recent = rolling(c, params["high_recent"], "max")
            hi_long = rolling(c, params["high_lookback"], "max")
            trending = hi_recent >= hi_long
            touch = (lo <= self.ema_fast) & (c >= self.ema_slow)
            fresh = np.ones(c.shape, dtype=bool)
            for k in range(1, params["fresh_sessions"] + 1):
                fresh &= shift(lo, k) > shift(self.ema_fast, k)
        self.setups = panel.valid & trend & trending & touch & fresh

    def candidates(self, t):
        return np.flatnonzero(self.setups[t])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        e = self.ema_slow[t_entry, j]
        if not np.isfinite(e):
            return None
        stop = float(e * (1 - self.params["stop_pct"] / 100))
        return stop if stop < entry_price else None

    def on_close(self, pos: Position, t):
        if t <= pos.entry_day:
            return None
        e = self.ema_slow[t, pos.j]
        c = self.panel.close[t, pos.j]
        if not (np.isfinite(e) and np.isfinite(c)):
            return None
        below = c < e
        if self.params["exit_rule"] == "first":
            return ("close", "close below 21-day EMA") if below else None
        state = pos.state
        if below:
            state["below"] = state.get("below", 0) + 1
            state["below_t"] = t
            if state["below"] >= 2:
                return ("close", "second close below 21-day EMA")
        elif "below_t" in state and t - state["below_t"] > self.params["reset_sessions"]:
            state["below"] = 0
        return None
