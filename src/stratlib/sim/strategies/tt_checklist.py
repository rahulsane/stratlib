"""The Traveling Trader's fundamentals checklist as a portfolio (see research/tt_checklist_fwd.py).

The runner precomputes, for the first session of each calendar quarter, the set of panel columns that pass
the checklist that day and stores it in `Checklist.passes` (rebalance row -> frozenset of columns).
Entry: at the close of a rebalance session, every passing eligible stock not already held (the engine ranks
extra signals by 63-session return and fills the free slots).
Initial stop: params["stop_pct"] below the entry price; not trailed.
Exit: at a rebalance close, a held stock that is not in that session's passing set (it failed the checklist,
or could not be evaluated) is sold.
"""

from __future__ import annotations

import numpy as np

from ..engine import Position, Strategy


class Checklist(Strategy):
    name = "tt_checklist"
    entry = "close"
    passes: dict[int, frozenset] = {}

    def setup(self, panel, params):
        super().setup(panel, params)
        self.rows = set(self.passes)

    def candidates(self, t):
        js = self.passes.get(t)
        return np.array(sorted(js), dtype=int) if js else np.zeros(0, dtype=int)

    def initial_stop(self, j, t_signal, entry_price, t_entry):
        return float(entry_price * (1 - self.params["stop_pct"] / 100))

    def size(self, j, t_signal, t, fill, stop, equity, available, orders_left):
        """params["equal_weight"]: split the cash on hand equally among this session's entries (the position
        cap in the rules still applies); otherwise the engine's risk-based size."""
        if not self.params.get("equal_weight"):
            return None
        return (available / max(orders_left, 1)) / fill

    def on_close(self, pos: Position, t):
        if t in self.rows and t > pos.entry_day and pos.j not in self.passes[t]:
            return ("close", "failed the checklist at a rebalance")
        return None
