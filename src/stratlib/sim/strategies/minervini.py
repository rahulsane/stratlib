"""Minervini-style trend template with a volatility contraction pattern (VCP), daily bars.

Trend template at the close of session T:
1. Close above the 50-, 150- and 200-day SMAs.
2. 50-day SMA above the 150-day; 150-day above the 200-day.
3. 200-day SMA above its value 21 sessions earlier.
4. Close at least 30% above the 52-week (252-session) low and within 25% of the 52-week high.
5. Relative strength in the top 30% of the day's universe (every stock and ETF passing the $5 and
   $20M rules). Score: 0.4 x the latest quarter's return + 0.2 x each of the three before it
   (quarters of 63 sessions).

VCP (swing points from a zigzag with a `swing_pct` reversal threshold, each confirmed only when the
reversal happens, so nothing later than T is used):
- Pullbacks: each swing high in the last `window` sessions to the lowest low after it (the next swing
  low, or the lowest low so far for the latest swing high).
- The latest pullback ends a run of at least `min_pullbacks` pullbacks, each shallower than the one
  before; it is no deeper than `final_max_pct`; its average volume (sessions after the swing high,
  through T) is below `volume_dry` x the 50-session average volume.
- The price has not traded above the latest swing high since; that high is the pivot.

Entry: buy-stop at the pivot (live for `order_life` sessions after the last setup day), filled at the
pivot or at the open on a gap. The trade is kept only if the breakout day's volume is at least
`breakout_volume` x the average of the previous 50 sessions; otherwise it is sold at that close.
Stop: the lowest low of the final pullback (through the session before entry), but no more than
`max_stop_pct` below the entry price. Exits: params["exit"] (exit_replay rules).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .exit_replay import ExitRules

C10 = {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10}
DEFAULTS = {
    "rs_min_pct": 70.0,
    "swing_pct": 3.0,
    "window": 60,
    "min_pullbacks": 2,
    "final_max_pct": 10.0,
    "volume_dry": 1.0,
    "breakout_volume": 1.4,
    "max_stop_pct": 8.0,
    "order_life": 5,
    "exit": C10,
}


def rolling(a, n, how):
    return getattr(pd.DataFrame(a).rolling(n, min_periods=n), how)().to_numpy()


def shift(a, k):
    if k == 0:
        return a
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def trend_template(panel, params) -> tuple[np.ndarray, np.ndarray]:
    """Conditions 1-5 and the RS percentile (0-100, NaN if not ranked)."""
    p = panel
    c = p.close
    sma50, sma150, sma200 = rolling(c, 50, "mean"), rolling(c, 150, "mean"), rolling(c, 200, "mean")
    low52, high52 = rolling(p.low, 252, "min"), rolling(p.high, 252, "max")
    cf = p.close_ff
    q = [shift(cf, 63 * k) for k in range(5)]
    with np.errstate(invalid="ignore", divide="ignore"):
        score = 0.4 * (q[0] / q[1] - 1) + 0.2 * (q[1] / q[2] - 1) + 0.2 * (q[2] / q[3] - 1) + 0.2 * (q[3] / q[4] - 1)
    score = np.where(p.eligible & np.isfinite(score), score, np.nan)
    rs = pd.DataFrame(score).rank(axis=1, pct=True).to_numpy() * 100
    with np.errstate(invalid="ignore"):
        template = (p.valid & (c > sma50) & (c > sma150) & (c > sma200) & (sma50 > sma150) & (sma150 > sma200)
                    & (sma200 > shift(sma200, 21)) & (c >= 1.3 * low52) & (c >= 0.75 * high52)
                    & (rs >= params["rs_min_pct"]))
    return template, rs


def vcp_scan(panel, template: np.ndarray, params: dict) -> dict:
    """Walk each security once, keeping a zigzag of confirmed swing points, and test the VCP on
    template days. Returns setup mask, pivot, swing-high index and diagnostics."""
    p = panel
    n_days, n_sec = p.close.shape
    x = params["swing_pct"] / 100
    window, need, final_max = params["window"], params["min_pullbacks"], params["final_max_pct"] / 100
    vol = np.nan_to_num(p.volume)
    vol_cum = np.vstack([np.zeros(n_sec), np.cumsum(vol, axis=0)])
    avg50 = rolling(p.volume, 50, "mean")
    setup = np.zeros((n_days, n_sec), dtype=bool)
    pivot = np.full((n_days, n_sec), np.nan)
    peak_at = np.full((n_days, n_sec), -1, dtype=np.int32)
    funnel = {"template_days": int(template.sum()), "two_or_more_contracting": 0, "final_within_limit": 0,
              "volume_dry": 0}
    for j in np.flatnonzero(template.any(axis=0)):
        high, low = p.high[:, j], p.low[:, j]
        swings = []  # alternating ("P"/"T", index, price), confirmed
        direction, hi_i, lo_i = 0, -1, -1
        for t in range(n_days):
            h, lo = high[t], low[t]
            if not (h == h and lo == lo):
                continue
            if direction == 0:
                if hi_i < 0:
                    hi_i = lo_i = t
                    continue
                if h > high[hi_i]:
                    hi_i = t
                elif lo <= high[hi_i] * (1 - x):
                    swings.append(("P", hi_i, high[hi_i]))
                    direction, lo_i = -1, t
                if direction == 0:
                    if lo < low[lo_i]:
                        lo_i = t
                    elif h >= low[lo_i] * (1 + x):
                        swings.append(("T", lo_i, low[lo_i]))
                        direction, hi_i = 1, t
            elif direction == 1:
                if h > high[hi_i]:
                    hi_i = t
                elif lo <= high[hi_i] * (1 - x):
                    swings.append(("P", hi_i, high[hi_i]))
                    direction, lo_i = -1, t
            else:
                if lo < low[lo_i]:
                    lo_i = t
                elif h >= low[lo_i] * (1 + x):
                    swings.append(("T", lo_i, low[lo_i]))
                    direction, hi_i = 1, t
            if not template[t, j] or direction == 0:
                continue
            # The latest swing high and the pullback after it.
            last_peaks = [k for k in range(len(swings) - 1, max(len(swings) - 3, -1), -1) if swings[k][0] == "P"]
            if not last_peaks:
                continue
            k_last = last_peaks[0]
            _, peak_i, peak_px = swings[k_last]
            if direction == 1 and high[hi_i] >= peak_px:
                continue  # already above the pivot
            final_low = low[lo_i] if direction == -1 else swings[k_last + 1][2]
            depths = []
            for k in range(k_last, -1, -1):
                kind, idx, px = swings[k]
                if kind != "P":
                    continue
                if idx < t - window + 1:
                    break
                trough = final_low if k == k_last else swings[k + 1][2]
                depths.append(1 - trough / px)
            depths.reverse()
            run = 1
            for a, b in zip(depths[-2::-1], depths[::-1]):
                if a > b:
                    run += 1
                else:
                    break
            if run < need:
                continue
            funnel["two_or_more_contracting"] += 1
            if depths[-1] > final_max:
                continue
            funnel["final_within_limit"] += 1
            days = t - peak_i
            if days < 1 or not (avg50[t, j] > 0):
                continue
            if (vol_cum[t + 1, j] - vol_cum[peak_i + 1, j]) / days >= params["volume_dry"] * avg50[t, j]:
                continue
            funnel["volume_dry"] += 1
            setup[t, j], pivot[t, j], peak_at[t, j] = True, peak_px, peak_i
    return {"setup": setup, "pivot": pivot, "peak_at": peak_at, "funnel": funnel}


def new_setups(setup: np.ndarray, pivot: np.ndarray) -> np.ndarray:
    """First session of each setup (the stock was not set up the session before at the same pivot)."""
    fresh = setup.copy()
    fresh[1:] &= ~(setup[:-1] & (pivot[:-1] == pivot[1:]))
    return fresh


class Minervini(ExitRules):
    name = "minervini_vcp"
    entry = "stop"

    def __init__(self, scan: dict | None = None):
        self.scan = scan  # reuse a vcp_scan result computed with the same setup parameters

    def setup(self, panel, params):
        params = {**DEFAULTS, **params}
        super().setup(panel, params)
        self.setup_exits(panel, params["exit"])
        self.order_life = params["order_life"]
        if self.scan is None:
            template, _ = trend_template(panel, params)
            self.scan = vcp_scan(panel, template, params)
        prev_avg = np.full_like(panel.close, np.nan)
        prev_avg[1:] = rolling(panel.volume, 50, "mean")[:-1]
        self.prev_avg_volume = prev_avg

    def orders(self, t):
        js = np.flatnonzero(self.scan["setup"][t])
        return {int(j): float(self.scan["pivot"][t, j]) for j in js}

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        peak = int(self.scan["peak_at"][t_signal, j])
        lows = self.panel.low[peak + 1:t_entry, j]
        if peak < 0 or not np.isfinite(lows).any():
            return None
        return max(float(np.nanmin(lows)), entry_price * (1 - self.params["max_stop_pct"] / 100))

    def on_close(self, pos, t):
        if t == pos.entry_day:
            avg, v = self.prev_avg_volume[t, pos.j], self.panel.volume[t, pos.j]
            if not (math.isfinite(avg) and math.isfinite(v) and v >= self.params["breakout_volume"] * avg):
                return ("close", f"breakout volume under {self.params['breakout_volume']:g}x")
        return super().on_close(pos, t)
