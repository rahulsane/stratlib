"""Qullamaggie-style breakout on daily bars.

Setup, checked at the close of session T (all windows end at T):
1. Prior move: the highest close of the last 60 sessions (session H; the
   latest one if tied) is at least 30% above the lowest low of the 60
   sessions before H.
2. Consolidation: 10 to 40 sessions since H. The lowest low since H is no more
   than 25% below H's close, and the range of the last 5 sessions (highest
   high minus lowest low) is under 12% of the close.
3. Trend: close above the 10- and 20-day SMAs; the 20-day SMA above its value
   5 sessions earlier.
4. Volatility: 20-day average daily range, (high - low) / low, of at least 4%.
Pivot: the highest high from H through T.

Entry: a buy-stop at the pivot, live on sessions T+1 to T+5 (a newer setup on
the same stock replaces it). Filled at the pivot, or at the open on a gap.
Initial stop: the low of the session before entry. The trade is skipped if
the stop is more than one 20-day average daily range (as of the session
before entry) below the entry price.
Exits: a third is sold at the close of the third session after entry and the
stop on the rest moves to the entry fill. From the next session on, the rest
is sold at the first close below the trailing SMA (10 or 20 days).

Entry rules (params["entry_rule"]); every rule keeps the one-ADR stop limit:
- pivot: the buy-stop above (the original test).
- strong_close: the first session that trades above the pivot decides. Buy at
  its close if the close is above the pivot and in the top third of the day's
  range; otherwise the order is dropped. Stop: that day's low.
- volume: as pivot, but sell at the entry day's close unless its volume is at
  least volume_mult x the average of the previous 50 sessions.
- strong_close_volume: strong_close, and the breakout day's volume must pass
  the volume test.
- orb5 / orb30: on a session that trades above the pivot, buy when a one-minute
  bar after the first 5 (or 30) minutes trades above the higher of the pivot
  and the opening-range high (at the bar's open if it opens above). Stop: the
  day's low up to the entry. One-minute data required (intraday.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..engine import Position, Strategy

DEFAULTS = {
    "lookback": 60,
    "prior_move_pct": 30.0,
    "consolidation_min": 10,
    "consolidation_max": 40,
    "max_depth_pct": 25.0,
    "tight_sessions": 5,
    "tight_range_pct": 12.0,
    "sma_fast": 10,
    "sma_slow": 20,
    "sma_rising_sessions": 5,
    "adr_sessions": 20,
    "min_adr_pct": 4.0,
    "order_life": 5,
    "max_stop_adr": 1.0,
    "partial_session": 3,
    "partial_fraction": 1 / 3,
    "trail_sma": 10,
    "entry_rule": "pivot",
    "strong_close_fraction": 2 / 3,
    "volume_mult": 1.5,
    "volume_sessions": 50,
}
CLOSE_RULES = ("strong_close", "strong_close_volume")
ORB_MINUTES = {"orb5": 5, "orb30": 30}


def shift(a: np.ndarray, k: int) -> np.ndarray:
    """a[t - k] at row t; NaN where t < k."""
    if k == 0:
        return a
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def rolling(a: np.ndarray, n: int, how: str) -> np.ndarray:
    """Full-window rolling statistic; NaN if any value in the window is missing."""
    frame = pd.DataFrame(a).rolling(n, min_periods=n)
    return getattr(frame, how)().to_numpy()


class Qullamaggie(Strategy):
    name = "qullamaggie_breakout"
    entry = "stop"

    def __init__(self, intraday=None):
        self.intraday = intraday  # intraday.Intraday, for the orb rules
        self.entry_info = {}  # (j, t) -> (stop, entry bar) for orb entries

    def setup(self, panel, params):
        params = {**DEFAULTS, **params}
        super().setup(panel, params)
        self.order_life = params["order_life"]
        self.entry = "close_confirm" if params["entry_rule"] in CLOSE_RULES else "stop"
        if params["entry_rule"] in ORB_MINUTES and self.intraday is None:
            raise ValueError("orb entry rules need one-minute data")
        self.avg_volume = shift(rolling(panel.volume, params["volume_sessions"], "mean"), 1)
        c, h, lo = panel.close, panel.high, panel.low
        n = params["lookback"]
        with np.errstate(invalid="ignore", divide="ignore"):
            # 1. The highest close of the last n sessions and how long ago it was.
            top = rolling(c, n, "max")
            since = np.full(c.shape, -1, dtype=np.int16)
            for k in range(n):
                since[(since < 0) & (shift(c, k) == top)] = k
            low_before = rolling(lo, n, "min")  # at row H-1: lowest low of the n sessions before H
            first, last = params["consolidation_min"], params["consolidation_max"]
            base_low = np.full(c.shape, np.nan)  # lowest low from H+1 to T
            pivot = np.full(c.shape, np.nan)  # highest high from H to T
            prior_low = np.full(c.shape, np.nan)  # lowest low of the n sessions before H
            low_min, high_max = lo.copy(), h.copy()
            for w in range(2, last + 2):
                low_min = np.minimum(low_min, shift(lo, w - 1))  # window of w sessions ending at T
                high_max = np.maximum(high_max, shift(h, w - 1))
                if first <= w <= last:
                    hit = since == w
                    base_low[hit] = low_min[hit]
                    prior_low[hit] = shift(low_before, w + 1)[hit]
                if first <= w - 1 <= last:
                    hit = since == w - 1
                    pivot[hit] = high_max[hit]
            prior_move = top >= (1 + params["prior_move_pct"] / 100) * prior_low
            # 2. Consolidation.
            in_range = (since >= first) & (since <= last)
            shallow = base_low >= (1 - params["max_depth_pct"] / 100) * top
            m = params["tight_sessions"]
            tight = (rolling(h, m, "max") - rolling(lo, m, "min")) / c < params["tight_range_pct"] / 100
            # 3. Trend.
            self.sma = {k: rolling(c, k, "mean") for k in {params["sma_fast"], params["sma_slow"], params["trail_sma"]}}
            fast, slow = self.sma[params["sma_fast"]], self.sma[params["sma_slow"]]
            trend = (c > fast) & (c > slow) & (slow > shift(slow, params["sma_rising_sessions"]))
            # 4. Volatility.
            self.adr = rolling(100 * (h - lo) / lo, params["adr_sessions"], "mean")
            volatile = self.adr >= params["min_adr_pct"]
        self.setups = panel.valid & prior_move & in_range & shallow & tight & trend & volatile & np.isfinite(pivot)
        self.pivot = pivot

    def orders(self, t):
        js = np.flatnonzero(self.setups[t])
        return {int(j): float(self.pivot[t, j]) for j in js}

    def volume_ok(self, j, t) -> bool:
        avg, vol = self.avg_volume[t, j], self.panel.volume[t, j]
        return bool(np.isfinite(avg) and np.isfinite(vol) and vol >= self.params["volume_mult"] * avg)

    def confirm(self, j, t_signal, t, trigger):
        p, rule = self.panel, self.params["entry_rule"]
        close, high, low = p.close[t, j], p.high[t, j], p.low[t, j]
        strong = close > trigger and high > low and (close - low) / (high - low) >= self.params["strong_close_fraction"]
        return bool(strong and (rule != "strong_close_volume" or self.volume_ok(j, t)))

    def fill_order(self, j, t_signal, t, trigger):
        rule = self.params["entry_rule"]
        if rule not in ORB_MINUTES:
            return super().fill_order(j, t_signal, t, trigger)
        if not self.panel.high[t, j] > trigger:
            return None
        day = self.intraday.day(j, t)
        if day is None:
            return None
        minutes = ORB_MINUTES[rule]
        opening = day.minute < minutes
        if not opening.any():
            return None
        level = max(trigger, float(day.high[opening].max()))
        for k in np.flatnonzero(day.minute >= minutes):
            if day.open[k] > level:
                price, note = float(day.open[k]), "opened above the opening-range trigger"
            elif day.high[k] > level:
                price, note = level, "through the opening-range trigger"
            else:
                continue
            self.entry_info[(j, t)] = (float(min(day.low[:k].min(), day.open[k])), int(k))
            return price, note
        return None

    def entry_day_stopped(self, pos, t, path):
        if self.params["entry_rule"] not in ORB_MINUTES:
            return super().entry_day_stopped(pos, t, path)
        day = self.intraday.day(pos.j, t)
        k = self.entry_info[(pos.j, t)][1]
        return bool(day.close[k] <= pos.stop or (day.low[k + 1:] <= pos.stop).any())

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        rule = self.params["entry_rule"]
        lows = self.panel.low[:t_entry, j]
        valid = np.flatnonzero(np.isfinite(lows))
        if not len(valid):
            return None
        prev = int(valid[-1])
        if rule in CLOSE_RULES:
            stop = float(self.panel.low[t_entry, j])
        elif rule in ORB_MINUTES:
            stop = self.entry_info[(j, t_entry)][0]
        else:
            stop = float(lows[prev])
        adr = self.adr[prev, j]
        limit = self.params["max_stop_adr"]  # None: no limit on the stop distance
        if limit is not None and (not np.isfinite(adr) or (entry_price - stop) / entry_price * 100 > limit * adr):
            return None
        return stop

    def on_close(self, pos: Position, t):
        params = self.params
        if t == pos.entry_day and params["entry_rule"] == "volume" and not self.volume_ok(pos.j, t):
            return ("close", f"breakout volume under {params['volume_mult']:g}x")
        if "partial_at" not in pos.state:
            if t - pos.entry_day >= params["partial_session"]:
                pos.state["partial_at"] = t
                pos.stop = pos.entry_fill
                return ("partial", "day-3 sale of a third", params["partial_fraction"])
            return None
        if t > pos.state["partial_at"]:
            sma = self.sma[params["trail_sma"]][t, pos.j]
            if np.isfinite(sma) and self.panel.close[t, pos.j] < sma:
                return ("close", f"close below {params['trail_sma']}-day SMA")
        return None
