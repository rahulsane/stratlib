"""Price panels for the Nash rule study: the ground-rules panel (2014 on) and a 2010-2016 holdout panel.

The holdout panel is built by panel.build with an earlier warm-up and study start. Its universe is today's
stocks plus FMP's directory of stocks delisted since 2011, which is thin before 2016, so it leans toward
survivors.
"""

from __future__ import annotations

import panel as P

HOLDOUT_FROM, HOLDOUT_STUDY, HOLDOUT_THROUGH = "2010-01-01", "2011-01-01", "2016-03-31"


def load_holdout(rebuild: bool = False) -> P.Panel:
    path = P.CACHE / f"panel_holdout_{HOLDOUT_STUDY}_{HOLDOUT_THROUGH}.npz"
    if path.exists() and not rebuild:
        import json
        import numpy as np
        data = np.load(path, allow_pickle=False)
        return P.Panel(dates=data["dates"], symbols=data["symbols"], kind=data["kind"], until=data["until"],
                       factor=data["factor"], notes=json.loads(str(data["notes"])), **{f: data[f] for f in P.FIELDS})
    saved = P.PANEL_FROM, P.STUDY_FROM
    P.PANEL_FROM, P.STUDY_FROM = HOLDOUT_FROM, HOLDOUT_STUDY
    try:
        panel = P.build(HOLDOUT_THROUGH)
    finally:
        P.PANEL_FROM, P.STUDY_FROM = saved
    P.save(panel, path)
    return panel


def rebalance_rows(p, start: str, end: str = "9999-12-31") -> list[int]:
    """First session of each calendar quarter in [start, end]."""
    return [i for i, d in enumerate(p.dates)
            if start <= d <= end and d[5:7] in ("01", "04", "07", "10") and i and p.dates[i - 1][:7] != d[:7]]


if __name__ == "__main__":
    p = load_holdout(rebuild=True)
    print({k: v for k, v in p.notes.items() if k not in ("split_history_missing", "duplicates", "cleaning")})
