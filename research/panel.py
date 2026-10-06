"""Builds and caches the research panel (cache/panel_<date>.npz). The panel itself, its cleaning and
its liquidity rules live in stratlib.sim.panel, shared with the app's backtests; they are imported here
under their old names."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from stratlib.app import open_context
from stratlib.sim.panel import (  # noqa: F401  (re-exported for the research scripts)
    PANEL_FROM,
    STUDY_FROM,
    MIN_PRICE,
    MIN_DOLLAR_VOLUME,
    ADV_SESSIONS,
    RANK_SESSIONS,
    FIELDS,
    Panel,
    _split_rows,
    _factor_column,
    _blank,
    clean_bad_bars,
    merge_duplicates,
    build,
)

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"


def save(panel: Panel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, dates=panel.dates, symbols=panel.symbols, kind=panel.kind, until=panel.until,
             factor=panel.factor, notes=np.array(json.dumps(panel.notes)),
             **{f: getattr(panel, f) for f in FIELDS})


def load(through: str | None = None, *, rebuild: bool = False, progress=print) -> Panel:
    """Cached panel through the latest SPY session (or `through`)."""
    ctx = open_context()
    try:
        latest = ctx.store.price_history("SPY", limit=1)[-1].date
    finally:
        ctx.close()
    through = min(through or latest, latest)
    path = CACHE / f"panel_{through}.npz"
    if path.exists() and not rebuild:
        data = np.load(path, allow_pickle=False)
        return Panel(dates=data["dates"], symbols=data["symbols"], kind=data["kind"], until=data["until"],
                     factor=data["factor"], notes=json.loads(str(data["notes"])),
                     **{f: data[f] for f in FIELDS})
    panel = build(through, progress=progress)
    save(panel, path)
    return panel


if __name__ == "__main__":
    p = load(rebuild=True)
    print(json.dumps({k: v for k, v in p.notes.items() if k != "split_history_missing"}, indent=2))
    print("split histories missing:", len(p.notes["split_history_missing"]), p.notes["split_history_missing"][:20])
