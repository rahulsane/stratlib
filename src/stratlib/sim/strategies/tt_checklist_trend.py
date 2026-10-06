"""Entry-only trend gates applied AFTER the unchanged D10 ranking.

The preceding session's split-adjusted close must strictly exceed its trailing
200-session simple moving average. All 200 closes must be valid; missing history
fails the relevant gate. Existing holdings keep D10's stops and quarterly exits.
Rejected names are not replaced with lower-ranked stocks. D10's cash-splitting
size hook operates on the remaining new orders, subject to its existing cap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .tt_checklist_top10 import RankedChecklist


def prior_session_trend(close, window=200):
    """Return lagged price, lagged SMA, and gate, without filling missing bars."""
    prices = pd.DataFrame(np.where(np.isfinite(close) & (close > 0), close, np.nan))
    prior = prices.shift(1).to_numpy()
    average = prices.rolling(window, min_periods=window).mean().shift(1).to_numpy()
    return prior, average, np.isfinite(prior) & np.isfinite(average) & (prior > average)


class TrendGatedChecklist(RankedChecklist):
    name = "tt_checklist_top10_trend"

    def setup(self, panel, params):
        super().setup(panel, params)
        self.prior_close, self.prior_sma, self.above_sma = prior_session_trend(
            panel.close, params["trend_sessions"])
        if params["market_gate"] and "SPY" not in panel.index:
            raise ValueError("The market gate requires SPY in the price panel")
        spy = panel.index.get("SPY")
        self.entry_passes = {}
        self.gate_audit = []
        for t, selected in sorted(self.passes.items()):
            market_ok = bool(self.above_sma[t, spy]) if spy is not None else False
            permitted = set()
            for j in sorted(selected):
                stock_ok = bool(self.above_sma[t, j])
                allowed = ((not params["market_gate"] or market_ok)
                           and (not params["stock_gate"] or stock_ok))
                if allowed:
                    permitted.add(j)
                self.gate_audit.append({
                    "rebalance_date": str(panel.dates[t]),
                    "signal_date": str(panel.dates[t - 1]) if t > 0 else "",
                    "ticker": str(panel.symbols[j]),
                    "prior_stock_close": float(self.prior_close[t, j]),
                    "prior_stock_sma200": float(self.prior_sma[t, j]),
                    "stock_above_sma200": stock_ok,
                    "prior_spy_close": float(self.prior_close[t, spy]) if spy is not None else np.nan,
                    "prior_spy_sma200": float(self.prior_sma[t, spy]) if spy is not None else np.nan,
                    "spy_above_sma200": market_ok,
                    "entry_permitted": allowed,
                })
            self.entry_passes[t] = frozenset(permitted)

    def candidates(self, t):
        return np.array(sorted(self.entry_passes.get(t, ())), dtype=int)
