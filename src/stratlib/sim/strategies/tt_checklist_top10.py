"""Quarterly top-N selection within the original trailing-fundamentals checklist.

Rank only that session's screen passers, by their trailing 63-session price return.
Missing momentum is excluded; equal scores are broken by ticker. A held stock that
falls out of the selected set is sold at the quarterly close. Retained positions
keep their shares and original stop. Entry sizing and stops are inherited from D.
"""

from __future__ import annotations

import numpy as np

from .tt_checklist import Checklist


def ranked_passers(panel, passes, top_n):
    selected, audit = {}, []
    for t, passing in sorted(passes.items()):
        ranked = sorted(
            (j for j in passing if panel.eligible[t, j] and np.isfinite(panel.ret63[t, j])),
            key=lambda j: (-float(panel.ret63[t, j]), str(panel.symbols[j])),
        )
        selected[t] = frozenset(ranked[:top_n])
        for rank, j in enumerate(ranked, 1):
            audit.append({
                "date": str(panel.dates[t]), "ticker": str(panel.symbols[j]),
                "rank": rank, "return_63_sessions_pct": float(panel.ret63[t, j] * 100),
                "selected": rank <= top_n, "screen_passers": len(passing),
            })
    return selected, audit


class RankedChecklist(Checklist):
    name = "tt_checklist_top10"

    def setup(self, panel, params):
        self.screen_passes = self.passes
        self.passes, self.rank_audit = ranked_passers(panel, self.screen_passes, params["top_n"])
        super().setup(panel, params)

    def on_close(self, pos, t):
        if t in self.rows and t > pos.entry_day and pos.j not in self.passes[t]:
            reason = ("fell outside top 10 at a rebalance" if pos.j in self.screen_passes[t]
                      else "failed the checklist at a rebalance")
            return ("close", reason)
        return None
