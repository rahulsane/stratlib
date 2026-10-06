"""Point-in-time Russell 3000 membership from iShares holdings saved by the Wayback Machine, for run_wtt_qs.py.

Sources: IWB (Russell 1000), IWM (Russell 2000) and IWV (Russell 3000) holdings files. IWM was saved about monthly,
IWB and IWV a few times a year. Russell rebuilds the index once a year (late June) and adds IPOs quarterly, so a
stock counts as a member in a week when each fund's latest snapshot, or any snapshot from the previous 190 days,
holds it (IWB went unsaved for up to a year at a time, and membership changes little between rebuilds). The window keeps the
stocks that moved from the Russell 2000 to the Russell 1000 at a reconstitution until an IWB snapshot shows them
(the strongest stocks, the ones a breakout rule wants); its cost is that stocks dropped from the index stay up to
190 days longer, the weakest and smallest, which a rule buying 20-week highs rarely touches.
Cache: research:wtt:russell ({fund: {date: [[ticker, name], ...]}}).

Run: PYTHONPATH="src;research" .venv/Scripts/python.exe research/wtt_russell.py [--refresh]
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from datetime import date

import numpy as np

import wtt_universe as U
from strategies.weekend_trend import week_ends

KEY = "research:wtt:russell"
FUNDS = {"IWB": "239707", "IWM": "239710", "IWV": "239714"}
SINCE, WINDOW_DAYS = "2015-06-01", 190
IWM_MONTHS = ("03", "06", "07", "09", "12")   # quarter-ends plus the month after reconstitution


def fetch(previous: dict | None = None) -> dict:
    """All snapshots; dates already in `previous` are kept rather than fetched again."""
    out: dict[str, dict] = {}
    kept = (previous or {}).get("funds", {})
    for fund, pid in FUNDS.items():
        cdx = "http://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
            [("url", f"ishares.com/us/products/{pid}/"), ("matchType", "prefix"), ("output", "json"),
             ("filter", "statuscode:200"), ("filter", "original:.*fileType=(json|csv).*")])
        captures = json.loads(U.get(cdx, timeout=300))[1:]
        wanted: dict[str, list[tuple[str, str]]] = {}
        for _, stamp, url, *_ in captures:
            m = re.search(r"tab=all&fileType=json&asOfDate=(\d{8})", url)
            if m:
                day = m.group(1)
            elif "fileType=csv" in url and "_holdings" in url and "asOfDate" not in url:
                day = stamp[:8]      # the holdings on the capture day
            else:
                continue
            iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            if iso < SINCE or (fund == "IWM" and day[4:6] not in IWM_MONTHS):
                continue
            wanted.setdefault(iso, []).append((stamp, url))
        snaps = {}
        print(fund, len(wanted), "dates to fetch", flush=True)
        for iso, options in sorted(wanted.items()):
            if iso in kept.get(fund, {}):
                snaps[iso] = kept[fund][iso]
                continue
            # Captures made long after the date can hold the product page instead (iShares stopped serving old
            # dates), so the earliest captures are tried first.
            for stamp, url in sorted(options)[:3]:
                try:
                    rows = U.parse_ijh(U.get(f"http://web.archive.org/web/{stamp}id_/{url}", timeout=60))
                except Exception as exc:
                    print(f"  {fund} {iso} ({stamp}): skipped ({type(exc).__name__})", flush=True)
                    time.sleep(3)
                    continue
                if len(rows) > (500 if fund == "IWB" else 1200):
                    snaps[iso] = rows
                print(f"  {fund} {iso}: {len(rows)} rows", flush=True)
                time.sleep(3)
                break
        out[fund] = snaps
    return {"fetched_on": date.today().isoformat(), "funds": out}


def load(refresh: bool = False) -> dict:
    """refresh: fetch the snapshots still missing (and new ones), keeping those already saved."""
    ctx = U.open_context()
    try:
        previous = ctx.store.document(KEY)
    finally:
        ctx.close()
    return U.load(KEY, lambda: fetch(previous), refresh)


def membership(panel, columns: U.Columns | None = None, refresh: bool = False) -> dict:
    """Boolean (sessions x columns) Russell 3000 mask, filled from each week's close to the next."""
    doc = load(refresh)
    columns = columns or U.Columns(panel)
    snaps = sorted((day, fund) for fund, s in doc["funds"].items() for day in s)
    cols: dict[tuple, set] = {}
    for day, fund in snaps:
        cols[(day, fund)] = {c for c in (columns(t, n, f"russell-{fund}", 0) for t, n in doc["funds"][fund][day])
                             if c is not None}
    n_days, n_cols = panel.close.shape
    mask = np.zeros((n_days, n_cols), dtype=bool)
    ends = np.flatnonzero(week_ends(panel.dates))
    days = [d for d, _ in snaps]
    for t in ends:
        day = str(panel.dates[t])
        hi = int(np.searchsorted(days, day, side="right"))
        members: set = set()
        latest_seen: set = set()
        for k in range(hi - 1, -1, -1):
            fund = snaps[k][1]
            recent = (date.fromisoformat(day) - date.fromisoformat(days[k])).days <= WINDOW_DAYS
            if recent or fund not in latest_seen:   # each fund's latest snapshot, however old, plus all recent ones
                members |= cols[snaps[k]]
            latest_seen.add(fund)
            if not recent and len(latest_seen) == len(FUNDS):
                break
        mask[t, sorted(members)] = True
    for a, b in zip(ends, list(ends[1:]) + [n_days]):
        mask[a + 1:b] = mask[a]
    sizes = {f"{fund} {day}": len(doc["funds"][fund][day]) for day, fund in snaps}
    matched = {f"{fund} {day}": len(cols[(day, fund)]) for day, fund in snaps}
    return {"r3000": mask, "sizes": sizes, "matched": matched}


if __name__ == "__main__":
    doc = load(refresh="--refresh" in sys.argv)
    for fund, s in doc["funds"].items():
        print(fund, len(s), "snapshots:", ", ".join(f"{d} ({len(r)})" for d, r in sorted(s.items())))
