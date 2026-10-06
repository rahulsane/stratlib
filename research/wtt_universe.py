"""Point-in-time S&P 500 and S&P MidCap 400 membership on the research panel, for run_wtt_sp900.py.

S&P 500: FMP's dated change log as resolved for the GARP study (research:garp:resolved2, garp_data.py).

S&P MidCap 400: FMP has no history. Two sources, combined:
- Wikipedia's "List of S&P 400 companies" as it stood at each week's close (the latest revision before
  20:00 UTC on the week's last session). Checked against IJH's holdings it agrees on 96-100% of names in
  2017-2018 and from 2023, but it was stale in 2016, early 2019 and 2020 (82-94%).
- IJH (iShares Core S&P Mid-Cap ETF) holdings saved by the Wayback Machine: 38 dates, quarterly from 2015 to
  2019, then sparse.
A stock counts as a member in a week when the Wikipedia list then in force has it, or the latest IJH snapshot
from the previous 190 days does. The union restores members a stale list missed; the names it keeps a little
too long are ones that just left the index, which a rule buying 20-week highs rarely touches.

Tickers map to panel columns by symbol, by the panel's merged renames, and by a hand-checked list of renames
(HAND, trusted without a name check); otherwise a ticker whose panel series belongs to another company (by
name) is left out, as RBC (Regal Beloit until 2021, RBC Bearings in the panel) is.
Cache: research:wtt:sp400 (Wikipedia revisions) and research:wtt:ijh (IJH snapshots).

Run: PYTHONPATH="src;research" .venv/Scripts/python.exe research/wtt_universe.py [--refresh]
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

import numpy as np

from stratlib.app import open_context
from garp_data import same_company
from strategies.weekend_trend import week_ends

SP400_KEY = "research:wtt:sp400"
IJH_KEY = "research:wtt:ijh"
GARP_KEY = "research:garp:resolved2"
PAGE = "List of S&P 400 companies"
SINCE = "2015-06-01"
IJH_DAYS = 190
USER_AGENT = "stratlib-research/1.0 (personal research)"
IJH_PATH = "ishares.com/us/products/239763/"
IJH_CSV = ("ishares.com/us/products/239763/ishares-core-sp-midcap-etf/1467271812596.ajax?fileType=csv"
           "&fileName=IJH_holdings&dataType=fund")
IJH_LATEST = "https://www.ishares.com/us/products/239763/ishares-core-s-p-mid-cap-etf/latest-holdings.csv"
TICKER = re.compile(r"\{\{\s*(?:nyse|nasdaq|bats|amex|nyse american|nyse mkt)[a-z ]*\|\s*([A-Za-z0-9.\-]+)\s*\}\}", re.I)
LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# Index ticker -> panel symbol for the same company, trusted without a name check: renamed tickers, and
# companies renamed under the same ticker (whose old names fail the check).
HAND = {
    "NNN": "NNN", "WAFD": "WAFD", "DNOW": "DNOW", "MD": "MD", "CNX": "CNX", "ATI": "ATI", "OI": "OI", "IRT": "IRT",
    "EXLS": "EXLS", "WAB": "WAB", "RH": "RH", "WW": "WW", "CXW": "CXW", "FICO": "FICO", "CDAY": "CDAY",
    "CCMP": "CCMP", "JW-A": "WLY", "JWA": "WLY", "CREE": "WOLF", "MLHR": "MLKN", "INT": "WKC", "POL": "AVNT",
    "DV": "ATGE", "ELY": "MODG", "AAXN": "AXON", "KAR": "OPLN", "GPS": "GAP", "ZI": "GTM", "HFC": "DINO",
    "WYND": "TNL", "HBHC": "HWC", "OZRK": "OZK", "APY": "CHX", "JCOM": "ZD", "FII": "FHI", "PLT": "POLY",
    "WTR": "WTRG", "ACXM": "RAMP", "HYH": "AVNS", "ERI": "CZR", "CSAL": "UNIT", "WETF": "WT", "ADS": "BFH",
    "HLS": "EHC", "JDSU": "VIAV", "GMT": "GATX", "HUB-B": "HUBB", "INCR": "SYNH", "SPW": "SPXC", "UA": "UAA",
    "DISCA": "WBD", "DISCK": "WBD",
}


def norm(ticker: str) -> str:
    return re.sub(r"[ ./]", "-", ticker.strip().upper())


def get(url: str, timeout: int = 120) -> bytes:
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:  # transient errors and rate limits: back off
            time.sleep(5 * 2 ** attempt)
    raise RuntimeError(f"failed: {url}")


def api(**params) -> dict:
    params.update(format="json", formatversion="2")
    return json.loads(get("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)))


# ----------------------------------------------------------------------
# Wikipedia


def cell_text(cell: str) -> str:
    cell = re.sub(r"^\s*style=\"[^\"]*\"\s*\|", "", cell)
    cell = LINK.sub(lambda m: m.group(2) or m.group(1), cell)
    return re.sub(r"\s+", " ", cell).strip()


def parse_table(text: str) -> list[list[str]]:
    """[ticker, company] rows of the page's first table (the constituents). The company is the cell after the
    ticker when the ticker comes first in the row, and the cell before it otherwise."""
    start = text.find("{|")
    end = text.find("\n|}", start)
    rows = []
    for chunk in text[start:end].split("\n|-")[1:]:
        cells = [c for c in re.split(r"\n\||\|\|", chunk) if c.strip() and not c.lstrip().startswith("!")]
        at = next((i for i, c in enumerate(cells) if TICKER.search(c)), None)
        if at is None:
            continue
        other = at + 1 if at == 0 else at - 1
        name = cell_text(cells[other]) if other < len(cells) else ""
        rows.append([norm(TICKER.search(cells[at]).group(1)), name])
    return rows


def fetch_wikipedia(sessions: list[str]) -> dict:
    """The revision in force at each week's close, and the parsed table of every such revision."""
    revs, cont = [], {}
    while True:
        d = api(action="query", prop="revisions", titles=PAGE, rvprop="ids|timestamp|size", rvlimit="500",
                rvdir="newer", rvstart=SINCE + "T00:00:00Z", **cont)
        revs += d["query"]["pages"][0]["revisions"]
        if "continue" not in d:
            break
        cont = d["continue"]
    stamps = [r["timestamp"] for r in revs]
    weeks = {}
    for day in sessions:
        k = int(np.searchsorted(stamps, day + "T20:00:00Z", side="right")) - 1
        if k >= 0:
            weeks[day] = revs[k]["revid"]
    needed = sorted(set(weeks.values()))
    tables = {}
    for i in range(0, len(needed), 20):
        d = api(action="query", prop="revisions", revids="|".join(map(str, needed[i:i + 20])),
                rvprop="ids|timestamp|content", rvslots="main")
        for rev in (r for p in d["query"]["pages"] for r in p["revisions"]):
            tables[str(rev["revid"])] = {"timestamp": rev["timestamp"],
                                         "rows": parse_table(rev["slots"]["main"]["content"])}
        time.sleep(1)
    return {"fetched_on": date.today().isoformat(), "weeks": weeks, "tables": tables, "revisions": len(revs)}


# ----------------------------------------------------------------------
# IJH holdings from the Wayback Machine


def parse_ijh(raw: bytes) -> list[list[str]]:
    """[ticker, name] equity rows of an iShares holdings file (the JSON or CSV format)."""
    if raw[:2] == bytes([0x1F, 0x8B]):     # some archived captures are stored gzip-compressed
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig", errors="ignore").lstrip()
    if text.startswith("{"):
        rows = json.loads(text)["aaData"]
        return [[norm(r[0]), str(r[1])] for r in rows
                if any(isinstance(x, str) and x.lower() == "equity" for x in r[2:5])]
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Ticker,"))
    return [[norm(r["Ticker"]), r.get("Name", "")] for r in csv.DictReader(io.StringIO("\n".join(lines[start:])))
            if (r.get("Asset Class") or "").lower() == "equity"]


def fetch_ijh() -> dict:
    cdx = "http://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": IJH_PATH, "matchType": "prefix", "output": "json", "filter": "statuscode:200"})
    captures = json.loads(get(cdx, timeout=300))[1:]
    wanted: dict[str, tuple[str, str]] = {}
    for _, stamp, url, *_ in captures:
        m = re.search(r"tab=all&fileType=json&asOfDate=(\d{8})", url)
        if m:
            wanted.setdefault(m.group(1), (stamp, url))
        elif "fileType=csv&fileName=IJH_holdings&dataType=fund" in url and "asOfDate" not in url:
            wanted.setdefault(stamp[:8], (stamp, url))   # the holdings on the capture day
    snapshots = {}
    for asof, (stamp, url) in sorted(wanted.items()):
        try:
            rows = parse_ijh(get(f"http://web.archive.org/web/{stamp}id_/{url}"))
        except Exception:
            continue
        if len(rows) > 300:
            snapshots[f"{asof[:4]}-{asof[4:6]}-{asof[6:]}"] = rows
        time.sleep(3)
    rows = parse_ijh(get(IJH_LATEST))
    snapshots[date.today().isoformat()] = rows
    return {"fetched_on": date.today().isoformat(), "snapshots": snapshots}


def load(key: str, fetch, refresh: bool):
    ctx = open_context()
    try:
        doc = ctx.store.document(key)
        if doc and not refresh:
            return doc
        doc = fetch()
        ctx.store.save_document(key, doc)
        return doc
    finally:
        ctx.close()


# ----------------------------------------------------------------------
# Panel columns


def panel_names(panel) -> dict[str, str]:
    """Company names for the panel's stocks: the symbols table, else the saved delisted profile."""
    ctx = open_context()
    try:
        con = sqlite3.connect(ctx.store.db_path)
        names = {s: n or "" for s, n in con.execute("select symbol, name from symbols")}
        con.close()
        for s, kind, until in zip(panel.symbols.tolist(), panel.kind.tolist(), panel.until.tolist()):
            if kind == "stock" and until and not names.get(s):
                profile = (ctx.store.document("backtest:approx:profile:" + s) or {}).get("profile") or {}
                names[s] = profile.get("companyName") or ""
        return names
    finally:
        ctx.close()


class Columns:
    """Index ticker (and company name) -> panel column, with a log of what could not be matched."""

    def __init__(self, panel):
        self.names = panel_names(panel)
        self.moved = {t: g["keep"] for g in panel.notes.get("duplicates", []) for t in g["merged"]}
        self.col = {s: j for j, (s, k) in enumerate(zip(panel.symbols.tolist(), panel.kind.tolist())) if k == "stock"}
        self.unmatched: dict[str, dict] = {}

    def __call__(self, ticker: str, name: str, source: str, weeks: int = 1) -> int | None:
        if HAND.get(ticker) in self.col:
            return self.col[HAND[ticker]]
        # iShares files write share classes without a separator (BRKB, MOGA).
        share_class = ticker[:-1] + "-" + ticker[-1] if re.fullmatch(r"[A-Z]{2,5}[ABCK]", ticker) else None
        for t in (ticker, self.moved.get(ticker), share_class):
            if t and t in self.col and same_company(name, self.names.get(t)):
                return self.col[t]
        u = self.unmatched.setdefault(f"{source}:{ticker}", {"name": name, "weeks": 0})
        u["weeks"] += weeks
        return None


def membership(panel, refresh: bool = False) -> dict:
    """Boolean (sessions x columns) masks sp500 and sp400 (S&P 400 filled from each week's close to the next),
    plus the sources' agreement and unmatched tickers."""
    n_days, n_cols = panel.close.shape
    dates = panel.dates.astype(str)
    columns = Columns(panel)
    ends = np.flatnonzero(week_ends(panel.dates))

    ctx = open_context()
    try:
        garp = ctx.store.document(GARP_KEY)
    finally:
        ctx.close()
    sp500 = np.zeros((n_days, n_cols), dtype=bool)
    study = int(np.searchsorted(dates, "2016-01-01"))
    for key, s in garp["series"].items():
        for a, b in s["spans"]:
            lo, hi = int(np.searchsorted(dates, a)), int(np.searchsorted(dates, b)) if b else n_days
            if hi <= study:
                continue
            j = next((c for c in (columns(t, s.get("name") or "", "sp500", 0) for t in [key, *s.get("tickers", [])])
                      if c is not None), None)
            if j is None:
                columns.unmatched.setdefault(f"sp500:{key}", {"name": s.get("name"), "weeks": 0})["weeks"] += \
                    (hi - max(lo, study)) // 5
                continue
            sp500[lo:hi, j] = True
    for k in [k for k, v in columns.unmatched.items() if k.startswith("sp500:") and v["weeks"] == 0]:
        del columns.unmatched[k]

    sessions = [str(d) for d in panel.dates[ends] if str(d) >= "2015-12-01"]
    wiki = load(SP400_KEY, lambda: fetch_wikipedia(sessions), refresh)
    ijh = load(IJH_KEY, fetch_ijh, refresh)
    snaps = sorted(ijh["snapshots"])
    wiki_cols: dict[str, set] = {}
    ijh_cols: dict[str, set] = {}
    sp400 = np.zeros((n_days, n_cols), dtype=bool)
    for t in ends:
        day = str(panel.dates[t])
        rev = wiki["weeks"].get(day)
        if rev is None:
            continue
        rev = str(rev)
        if rev not in wiki_cols:
            wiki_cols[rev] = {c for c in (columns(tk, nm, "wikipedia", 0) for tk, nm in wiki["tables"][rev]["rows"])
                              if c is not None}
        cols = set(wiki_cols[rev])
        k = int(np.searchsorted(snaps, day, side="right")) - 1
        if k >= 0 and (date.fromisoformat(day) - date.fromisoformat(snaps[k])).days <= IJH_DAYS:
            s = snaps[k]
            if s not in ijh_cols:
                ijh_cols[s] = {c for c in (columns(tk, nm, "ijh", 0) for tk, nm in ijh["snapshots"][s]) if c is not None}
            cols |= ijh_cols[s]
        sp400[t, sorted(cols)] = True
    for a, b in zip(ends, list(ends[1:]) + [n_days]):
        sp400[a + 1:b] = sp400[a]
    # Weeks each unmatched S&P 400 ticker was listed (Wikipedia), for the report.
    for t in ends:
        rev = wiki["weeks"].get(str(panel.dates[t]))
        if rev is not None and str(panel.dates[t]) >= "2016-01-01":
            for tk, _ in wiki["tables"][str(rev)]["rows"]:
                if f"wikipedia:{tk}" in columns.unmatched:
                    columns.unmatched[f"wikipedia:{tk}"]["weeks"] += 1
    return {"sp500": sp500, "sp400": sp400, "unmatched": columns.unmatched,
            "agreement": agreement(wiki, ijh), "table_sizes": [len(v["rows"]) for v in wiki["tables"].values()]}


def agreement(wiki: dict, ijh: dict) -> list[dict]:
    """For each IJH snapshot: its names, the Wikipedia list then in force, and how far they agree (by ticker)."""
    weeks = sorted(wiki["weeks"])
    out = []
    for day, rows in sorted(ijh["snapshots"].items()):
        k = int(np.searchsorted(weeks, day, side="right")) - 1
        if k < 0:
            continue
        w = {t for t, _ in wiki["tables"][str(wiki["weeks"][weeks[k]])]["rows"]}
        i = {t for t, _ in rows} - {"-"}
        out.append({"date": day, "ijh": len(i), "wikipedia": len(w), "both": len(w & i),
                    "wikipedia_only": len(w - i), "ijh_only": len(i - w)})
    return out


if __name__ == "__main__":
    import panel as P
    p = P.load(through="2026-09-30")
    m = membership(p, refresh="--refresh" in sys.argv)
    ends = np.flatnonzero(week_ends(p.dates))
    ends = ends[p.dates[ends] >= "2016-01-01"]
    n5, n4 = m["sp500"][ends].sum(axis=1), m["sp400"][ends].sum(axis=1)
    union = (m["sp500"] | m["sp400"])[ends].sum(axis=1)
    print(f"weekly members in the panel: S&P 500 {n5.min()}-{n5.max()} (median {int(np.median(n5))}); S&P 400 "
          f"{n4.min()}-{n4.max()} (median {int(np.median(n4))}); union {union.min()}-{union.max()} (median "
          f"{int(np.median(union))})")
    for a in m["agreement"]:
        print(f"  {a['date']}: IJH {a['ijh']}, Wikipedia {a['wikipedia']}, both {a['both']} "
              f"({100 * a['both'] / a['ijh']:.0f}%)")
    worst = sorted(m["unmatched"].items(), key=lambda kv: -kv[1]["weeks"])[:25]
    for k, v in worst:
        print(f"  unmatched {k:24} {v['weeks']:4} weeks  {v['name']!s:.45}")
