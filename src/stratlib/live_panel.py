"""A dated price panel built from the local cache, with the arrays the scan rules read.

One row per completed SPY session, one column per stock. The attribute names match
research/panel.py's ``Panel`` so the live rules in setups.py can be checked against the research
strategies on the same arrays (tests/test_setups_parity.py). The live panel holds stocks only and
treats the cached split-adjusted close as the as-traded price: the newest session is always as
traded, and a split inside the window would change the $5 floor on older rows only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ADV_SESSIONS = 20
RANK_SESSIONS = 63


@dataclass
class LivePanel:
    dates: np.ndarray        # ISO strings, completed SPY sessions, ascending
    symbols: np.ndarray
    open: np.ndarray         # [session, stock]; NaN where there is no bar
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    min_price: float = 5.0
    min_dollar_volume: float = 20e6
    factor: np.ndarray | None = None  # adjusted price * factor = as-traded price

    def __post_init__(self):
        if self.factor is None:
            self.factor = np.ones_like(self.close)
        self.index = {str(s): j for j, s in enumerate(self.symbols)}
        self.day = {str(d): i for i, d in enumerate(self.dates)}
        valid = np.isfinite(self.close) & (self.close > 0)
        self.valid = valid
        closes = pd.DataFrame(np.where(valid, self.close, np.nan))
        self.close_ff = closes.ffill(limit=5).to_numpy()
        dollar = pd.DataFrame(np.where(valid, self.close * np.nan_to_num(self.volume), np.nan))
        self.adv20 = dollar.rolling(ADV_SESSIONS, min_periods=15).mean().to_numpy()
        self.as_traded_close = self.close * self.factor
        with np.errstate(invalid="ignore", divide="ignore"):
            lag = np.full_like(self.close_ff, np.nan)
            lag[RANK_SESSIONS:] = self.close_ff[:-RANK_SESSIONS]
            self.ret63 = self.close_ff / lag - 1
            self.eligible = valid & (self.as_traded_close >= self.min_price) & (self.adv20 >= self.min_dollar_volume)

    @property
    def last(self) -> int:
        return len(self.dates) - 1

    @classmethod
    def load(cls, store, symbols: list[str], sessions: list[str], *, min_price: float = 5.0,
             min_dollar_volume: float = 20e6) -> "LivePanel":
        """Bars for ``symbols`` on exactly ``sessions`` (a stock's bars on other dates are ignored)."""
        if not sessions:
            raise ValueError("A panel needs at least one session.")
        row = {d: i for i, d in enumerate(sessions)}
        column = {s: j for j, s in enumerate(symbols)}
        arrays = {name: np.full((len(sessions), len(symbols)), np.nan) for name in
                  ("open", "high", "low", "close", "volume")}
        for symbol, day, o, h, lo, c, v in store.price_panel_rows(symbols, sessions[0], sessions[-1]):
            i = row.get(day)
            if i is None:
                continue
            j = column[symbol]
            for name, value in zip(("open", "high", "low", "close", "volume"), (o, h, lo, c, v)):
                if value is not None:
                    arrays[name][i, j] = value
        return cls(np.array(sessions), np.array(symbols), **arrays, min_price=min_price,
                   min_dollar_volume=min_dollar_volume)

    def select(self, columns: np.ndarray) -> "LivePanel":
        """The same sessions for a subset of stocks, to keep the heavy rules off illiquid names."""
        return LivePanel(self.dates, self.symbols[columns], self.open[:, columns], self.high[:, columns],
                         self.low[:, columns], self.close[:, columns], self.volume[:, columns],
                         self.min_price, self.min_dollar_volume, self.factor[:, columns])
