"""Signal rules of the scan strategies, as pure array functions over a LivePanel.

Each function is the research strategy's signal code (research/strategies/*.py, research/
market_filters.py) with its parameters read from a saved dataclass. They are not reimplemented:
tests/test_setups_parity.py runs the research code and these on the same arrays and requires
identical masks, so the live screen and the published backtests apply the same rules.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def shift(a: np.ndarray, k: int) -> np.ndarray:
    """a[t - k] at row t; NaN where t < k."""
    if k == 0:
        return a
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def rolling(a: np.ndarray, n: int, how: str) -> np.ndarray:
    """Full-window rolling statistic; NaN if any value in the window is missing."""
    return getattr(pd.DataFrame(a).rolling(n, min_periods=n), how)().to_numpy()


def ema(a: np.ndarray, span: int) -> np.ndarray:
    return pd.DataFrame(a).ewm(span=span, adjust=False, min_periods=span).mean().to_numpy()


# ----------------------------------------------------------------------------------------------
# Qullamaggie breakout

def qullamaggie_setups(panel, p) -> dict:
    """Setup mask, buy-stop pivot, average daily range, moving averages and the prior move's inputs."""
    c, h, lo = panel.close, panel.high, panel.low
    n = p.lookback
    with np.errstate(invalid="ignore", divide="ignore"):
        top = rolling(c, n, "max")           # highest close of the last n sessions, session H
        since = np.full(c.shape, -1, dtype=np.int16)
        for k in range(n):
            since[(since < 0) & (shift(c, k) == top)] = k
        low_before = rolling(lo, n, "min")   # at row H-1: lowest low of the n sessions before H
        first, last = p.consolidation_min, p.consolidation_max
        base_low = np.full(c.shape, np.nan)  # lowest low from H+1 to T
        pivot = np.full(c.shape, np.nan)     # highest high from H to T
        prior_low = np.full(c.shape, np.nan)
        low_min, high_max = lo.copy(), h.copy()
        for w in range(2, last + 2):
            low_min = np.minimum(low_min, shift(lo, w - 1))
            high_max = np.maximum(high_max, shift(h, w - 1))
            if first <= w <= last:
                hit = since == w
                base_low[hit] = low_min[hit]
                prior_low[hit] = shift(low_before, w + 1)[hit]
            if first <= w - 1 <= last:
                hit = since == w - 1
                pivot[hit] = high_max[hit]
        prior_move = top >= (1 + p.prior_move_pct / 100) * prior_low
        in_range = (since >= first) & (since <= last)
        shallow = base_low >= (1 - p.max_depth_pct / 100) * top
        m = p.tight_sessions
        tight = (rolling(h, m, "max") - rolling(lo, m, "min")) / c < p.tight_range_pct / 100
        sma = {k: rolling(c, k, "mean") for k in {p.sma_fast, p.sma_slow, p.trail_sma}}
        fast, slow = sma[p.sma_fast], sma[p.sma_slow]
        trend = (c > fast) & (c > slow) & (slow > shift(slow, p.sma_rising_sessions))
        adr = rolling(100 * (h - lo) / lo, p.adr_sessions, "mean")
        volatile = adr >= p.min_adr_pct
    setups = panel.valid & prior_move & in_range & shallow & tight & trend & volatile & np.isfinite(pivot)
    return {"setups": setups, "pivot": pivot, "adr": adr, "sma": sma, "since": since, "top": top,
            "prior_low": prior_low}


# ----------------------------------------------------------------------------------------------
# Minervini trend template and VCP

def minervini_template(panel, p) -> tuple[np.ndarray, np.ndarray]:
    """Trend template conditions 1-5 and the RS percentile (0-100, NaN if not ranked)."""
    c = panel.close
    sma50, sma150, sma200 = rolling(c, 50, "mean"), rolling(c, 150, "mean"), rolling(c, 200, "mean")
    low52, high52 = rolling(panel.low, 252, "min"), rolling(panel.high, 252, "max")
    cf = panel.close_ff
    q = [shift(cf, 63 * k) for k in range(5)]
    with np.errstate(invalid="ignore", divide="ignore"):
        score = 0.4 * (q[0] / q[1] - 1) + 0.2 * (q[1] / q[2] - 1) + 0.2 * (q[2] / q[3] - 1) + 0.2 * (q[3] / q[4] - 1)
    score = np.where(panel.eligible & np.isfinite(score), score, np.nan)
    rs = pd.DataFrame(score).rank(axis=1, pct=True).to_numpy() * 100
    with np.errstate(invalid="ignore"):
        template = (panel.valid & (c > sma50) & (c > sma150) & (c > sma200) & (sma50 > sma150) & (sma150 > sma200)
                    & (sma200 > shift(sma200, 21)) & (c >= 1.3 * low52) & (c >= 0.75 * high52)
                    & (rs >= p.rs_min_pct))
    return template, rs


def minervini_vcp(panel, template: np.ndarray, p) -> dict:
    """Walk each stock once, keeping a zigzag of confirmed swing points, and test the VCP on
    template days. Returns the setup mask, pivot and the session index of the swing high."""
    n_days, n_sec = panel.close.shape
    x = p.swing_pct / 100
    window, need, final_max = p.window, p.min_pullbacks, p.final_max_pct / 100
    vol = np.nan_to_num(panel.volume)
    vol_cum = np.vstack([np.zeros(n_sec), np.cumsum(vol, axis=0)])
    avg50 = rolling(panel.volume, 50, "mean")
    setup = np.zeros((n_days, n_sec), dtype=bool)
    pivot = np.full((n_days, n_sec), np.nan)
    peak_at = np.full((n_days, n_sec), -1, dtype=np.int32)
    depth_at = np.full((n_days, n_sec), np.nan)
    count_at = np.zeros((n_days, n_sec), dtype=np.int16)
    for j in np.flatnonzero(template.any(axis=0)):
        high, low = panel.high[:, j], panel.low[:, j]
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
            if depths[-1] > final_max:
                continue
            days = t - peak_i
            if days < 1 or not (avg50[t, j] > 0):
                continue
            if (vol_cum[t + 1, j] - vol_cum[peak_i + 1, j]) / days >= p.volume_dry * avg50[t, j]:
                continue
            setup[t, j], pivot[t, j], peak_at[t, j] = True, peak_px, peak_i
            depth_at[t, j], count_at[t, j] = depths[-1], run
    return {"setup": setup, "pivot": pivot, "peak_at": peak_at, "final_depth": depth_at, "contractions": count_at}


# ----------------------------------------------------------------------------------------------
# Episodic pivot

def earnings_mask(panel, dates_by_symbol: dict[str, list[str]]) -> np.ndarray:
    """[session, stock]: the session is the first one after an earnings release (either time of day)."""
    mask = np.zeros(panel.close.shape, dtype=bool)
    sessions = panel.dates
    for symbol, dates in dates_by_symbol.items():
        j = panel.index.get(symbol)
        if j is None:
            continue
        for d in dates:
            t = int(np.searchsorted(sessions, d))  # first session on or after the release date
            if t >= len(sessions):
                continue
            mask[t, j] = True  # before the open on a session day, or any time on a non-session day
            if sessions[t] == d and t + 1 < len(sessions):
                mask[t + 1, j] = True  # after the close
    return mask


def episodic_signals(panel, p, earnings: np.ndarray | None) -> np.ndarray:
    """Gap, volume, earnings and neglect conditions, without the entry or liquidity rules."""
    prev_close = np.full_like(panel.close_ff, np.nan)
    prev_close[1:] = panel.close_ff[:-1]
    n = p.volume_sessions
    avg_volume = np.full_like(panel.close, np.nan)
    avg_volume[1:] = pd.DataFrame(panel.volume).rolling(n, min_periods=n).mean().to_numpy()[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = panel.open / prev_close - 1 >= p.gap_pct / 100
        volume = panel.volume >= p.volume_mult * avg_volume
        signal = panel.valid & gap & volume
        if p.earnings:
            signal &= earnings
        if p.neglected:
            k = p.neglect_sessions
            before = np.full_like(panel.close_ff, np.nan)
            before[k + 1:] = panel.close_ff[:-(k + 1)]
            signal &= prev_close / before - 1 < p.neglect_max_pct / 100
    return signal


def strong_close(panel, fraction: float) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        rng = panel.high - panel.low
        return (panel.close > panel.open) & (rng > 0) & ((panel.close - panel.low) / rng >= fraction)


def prior_liquidity(panel) -> np.ndarray:
    """Liquidity measured before the signal day: the 20 sessions before it and the prior close.

    The research rule also dropped stocks whose split adjustment varies over 100-fold since 2016; the
    live panel has no split history, so that data-quality guard is not applied here.
    """
    adv_prior = np.full_like(panel.adv20, np.nan)
    adv_prior[1:] = panel.adv20[:-1]
    close_prior = np.full_like(panel.close_ff, np.nan)
    close_prior[1:] = (panel.close_ff * panel.factor)[:-1]
    with np.errstate(invalid="ignore"):
        return (adv_prior >= panel.min_dollar_volume) & (close_prior >= panel.min_price)


# ----------------------------------------------------------------------------------------------
# 9/21 EMA pullback

def ema_pullback_setups(panel, p) -> dict:
    c, lo = panel.close, panel.low
    with np.errstate(invalid="ignore"):
        fast, slow = ema(c, p.ema_fast), ema(c, p.ema_slow)
        sma_mid, sma_long = rolling(c, p.sma_mid, "mean"), rolling(c, p.sma_long, "mean")
        trend = (fast > slow) & (slow > sma_mid) & (c > sma_long)
        trending = rolling(c, p.high_recent, "max") >= rolling(c, p.high_lookback, "max")
        touch = (lo <= fast) & (c >= slow)
        fresh = np.ones(c.shape, dtype=bool)
        for k in range(1, p.fresh_sessions + 1):
            fresh &= shift(lo, k) > shift(fast, k)
    return {"setups": panel.valid & trend & trending & touch & fresh, "ema_fast": fast, "ema_slow": slow}


# ----------------------------------------------------------------------------------------------
# Market filters (research/market_filters.py)

def market_filter_masks(panel, spy_close: np.ndarray, qqq_close: np.ndarray) -> dict:
    """One boolean per session for filters A-F. ``spy_close``/``qqq_close`` are forward-filled
    closes on the panel's sessions; breadth uses the panel's eligible stocks."""
    def sma(x, n):
        return pd.DataFrame(x).rolling(n, min_periods=n).mean().to_numpy()

    spy_sma50 = sma(spy_close[:, None], 50)[:, 0]
    qqq_10, qqq_20 = sma(qqq_close[:, None], 10)[:, 0], sma(qqq_close[:, None], 20)[:, 0]
    close, high, low, eligible = panel.close, panel.high, panel.low, panel.eligible
    with np.errstate(invalid="ignore"):
        sma50 = sma(close, 50)
        counted = eligible & np.isfinite(sma50)
        breadth = (counted & (close > sma50)).sum(axis=1) / np.maximum(counted.sum(axis=1), 1)
        prior_high = np.full_like(high, np.nan)
        prior_low = np.full_like(low, np.nan)
        prior_high[1:] = pd.DataFrame(high).rolling(251, min_periods=251).max().to_numpy()[:-1]
        prior_low[1:] = pd.DataFrame(low).rolling(251, min_periods=251).min().to_numpy()[:-1]
        new_highs = (eligible & (high > prior_high)).sum(axis=1)
        new_lows = (eligible & (low < prior_low)).sum(axis=1)
        net = pd.Series(new_highs - new_lows, dtype=float).rolling(10, min_periods=10).mean().to_numpy()
        masks = {"A": np.ones(len(panel.dates), dtype=bool), "B": spy_close > spy_sma50,
                 "C": qqq_10 > qqq_20, "D": breadth > 0.5, "E": net > 0}
    masks["F"] = masks["B"] & masks["D"]
    return {"masks": masks, "breadth": breadth, "net_highs_lows": net, "spy_sma50": spy_sma50}
