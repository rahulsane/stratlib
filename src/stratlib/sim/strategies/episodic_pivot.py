"""Episodic pivot on daily bars.

Signal on session T:
1. Open at least gap_pct above the prior close.
2. Volume at least volume_mult x the average of the previous 50 sessions.
3. (earnings=True) T is the first session after an earnings release. FMP's dates have no time of
   day, so a release dated on the previous session (after the close) or on T (before the open)
   qualifies, as does one dated on a weekend or holiday just before T.
4. (neglected=True) The return over the 60 sessions before T (to the prior close) is under 20%.
Entry: buy at T's close if the close is above the open and in the upper half of T's range.
Stop: T's low. Exits: params["exit"] (see exit_replay.py).
Liquidity: the ground rules check the signal day itself; params["liquidity"] = "prior" uses the 20
sessions before it and the prior close instead, and drops stocks with extreme split histories.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .exit_replay import ExitRules

C10 = {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10}
DEFAULTS = {
    "gap_pct": 10.0,
    "volume_mult": 3.0,
    "volume_sessions": 50,
    "earnings": True,
    "neglected": False,
    "neglect_sessions": 60,
    "neglect_max_pct": 20.0,
    "close_in_range": 0.5,
    "exit": C10,
}


def earnings_mask(panel, dates_by_symbol: dict[str, list[str]]) -> np.ndarray:
    """[session, security]: the session is the first one after a release (either time of day)."""
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


def signal_mask(panel, params: dict, earnings: np.ndarray | None) -> np.ndarray:
    """Conditions 1-4 (without the entry condition or the liquidity rule)."""
    p = panel
    prev_close = np.full_like(p.close_ff, np.nan)
    prev_close[1:] = p.close_ff[:-1]
    n = params["volume_sessions"]
    avg_volume = np.full_like(p.close, np.nan)
    avg_volume[1:] = pd.DataFrame(p.volume).rolling(n, min_periods=n).mean().to_numpy()[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = p.open / prev_close - 1 >= params["gap_pct"] / 100
        volume = p.volume >= params["volume_mult"] * avg_volume
        signal = p.valid & gap & volume
        if params["earnings"]:
            signal &= earnings
        if params["neglected"]:
            k = params["neglect_sessions"]
            before = np.full_like(p.close_ff, np.nan)
            before[k + 1:] = p.close_ff[:-(k + 1)]
            signal &= prev_close / before - 1 < params["neglect_max_pct"] / 100
    return signal


def strong_close(panel, fraction: float) -> np.ndarray:
    p = panel
    with np.errstate(invalid="ignore", divide="ignore"):
        rng = p.high - p.low
        return (p.close > p.open) & (rng > 0) & ((p.close - p.low) / rng >= fraction)


def prior_liquidity(panel, params: dict) -> np.ndarray:
    """liquidity="prior": 20-day average dollar volume over the 20 sessions before the signal day of at
    least min_dollar_volume (default $20M), prior close (as traded) of at least min_price (default $5), and no
    stock whose split adjustment varies more than max_split_multiple-fold over the study period
    (split_window_start on)."""
    p = panel
    adv_prior = np.full_like(p.adv20, np.nan)
    adv_prior[1:] = p.adv20[:-1]
    close_prior = np.full_like(p.close_ff, np.nan)
    close_prior[1:] = (p.close_ff * p.factor)[:-1]
    window = p.factor[p.dates >= params.get("split_window_start", "2016-01-01")]
    multiple = np.nanmax(window, axis=0) / np.nanmin(window, axis=0)
    with np.errstate(invalid="ignore"):
        return ((adv_prior >= params.get("min_dollar_volume", 20e6)) & (close_prior >= params.get("min_price", 5.0))
                & (multiple <= params.get("max_split_multiple", 100))[None, :])


class EpisodicPivot(ExitRules):
    name = "episodic_pivot"
    entry = "close"

    def __init__(self, earnings_dates: dict[str, list[str]] | None = None):
        self.earnings_dates = earnings_dates

    def setup(self, panel, params):
        params = {**DEFAULTS, **params}
        super().setup(panel, params)
        self.setup_exits(panel, params["exit"])
        earnings = earnings_mask(panel, self.earnings_dates) if params["earnings"] else None
        self.signals = signal_mask(panel, params, earnings)
        self.entries = self.signals & strong_close(panel, params["close_in_range"])
        if params.get("liquidity", "signal_day") == "prior":
            self.eligible_mask = prior_liquidity(panel, params)

    def candidates(self, t):
        return np.flatnonzero(self.entries[t])

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return float(self.panel.low[t_entry, j])
