"""Data for the S&P 500 GARP replication (garp_index.py, garp_backtest.py).

Membership: FMP's current S&P 500 list plus its change log (historical-sp500-constituent), walked backwards
to 2004 and turned into in-index intervals per ticker. A change dated D takes effect before D's open: the added
stock is a member from D, the removed stock's last session in the index is the one before D.

Prices: the app database (split-adjusted bars) where it covers at least 90% of a member's sessions in the
index and the company name matches; otherwise FMP daily bars fetched for the ticker itself
(research:garp:px:<ticker>), accepted on the same tests. Members with neither are reported as missing; they
cannot be held, so they are left out of the universe.

Statements (research:garp:stmts:<symbol>): 30 annual income statements, 100 quarterly income statements and
100 quarterly balance sheets, keeping the fields the index reads. FMP restates per-share figures for later
splits, so EPS and shares share one basis with the split-adjusted prices.

Dividends: research:nash:div:<symbol> (FMP adjDividend by ex-date), shared with nash_screen.py.
Splits: backtest:splits:<symbol> where the app has it, otherwise research:garp:splits:<symbol>.

Run: PYTHONPATH=src .venv/Scripts/python research/garp_data.py [--refresh-members]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from stratlib.app import open_context
from stratlib.fmp.errors import FMPAuthError, FMPError

SINCE = "2004-01-01"
MEMBERS_KEY = "research:garp:sp500"
PX_KEY = "research:garp:px:"
STMT_KEY = "research:garp:stmts:"
SPLIT_KEY = "research:garp:splits:"
APP_SPLIT_KEY = "backtest:splits:"
DIV_KEY = "research:nash:div:"
RESOLVED_KEY = "research:garp:resolved2"
MIN_COVERAGE = 0.90

FIELDS = {
    "income_annual": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency", "revenue",
                      "netIncome", "eps", "epsDiluted", "weightedAverageShsOut", "weightedAverageShsOutDil",
                      "interestIncome", "interestExpense"),
    "income_quarter": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency", "revenue",
                       "netIncome", "eps", "epsDiluted", "weightedAverageShsOut", "weightedAverageShsOutDil",
                       "interestIncome", "interestExpense"),
    "balance_quarter": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency",
                        "totalDebt", "totalStockholdersEquity", "totalEquity", "minorityInterest",
                        "preferredStock"),
}
CALLS = {"income_annual": ("income-statement", "annual", 30),
         "income_quarter": ("income-statement", "quarter", 100),
         "balance_quarter": ("balance-sheet-statement", "quarter", 100)}

STOP = {"inc", "corp", "corporation", "company", "co", "the", "plc", "ltd", "limited", "holdings", "holding",
        "group", "incorporated", "class", "common", "stock", "sa", "nv", "lp", "and", "new", "de", "intl",
        "international", "shares", "ordinary", "series", "llc", "trust", "cl", "com", "ag", "se"}


def tokens(name: str | None) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return frozenset(w for w in words if w not in STOP and len(w) > 1)


def same_company(a: str | None, b: str | None) -> bool:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return True                      # nothing to compare against
    if ta & tb:
        return True
    return any(x[:4] == y[:4] for x in ta for y in tb if len(x) >= 4 and len(y) >= 4)


# ----------------------------------------------------------------------------------------------
# Membership

@dataclass
class Member:
    ticker: str
    names: set = field(default_factory=set)
    spans: list = field(default_factory=list)        # [start, end) ISO dates; end "" while still a member

    def in_index(self, day: str) -> bool:
        return any(a <= day and (not b or day < b) for a, b in self.spans)


def load_members(ctx, refresh: bool = False) -> dict:
    doc = ctx.store.document(MEMBERS_KEY)
    if doc and not refresh:
        return doc
    events = ctx.client.get("historical-sp500-constituent", {})
    current = ctx.client.get("sp500-constituent", {})
    doc = {"fetched_on": date.today().isoformat(), "events": events, "current": current}
    ctx.store.save_document(MEMBERS_KEY, doc)
    return doc


def membership(doc: dict, since: str = SINCE) -> dict[str, Member]:
    current = {x["symbol"]: x.get("name") for x in doc["current"]}
    events = sorted(doc["events"], key=lambda e: e["date"])
    members = set(current)
    for e in reversed(events):                       # back to `since`
        if e["date"] < since:
            break
        a, r = (e.get("symbol") or "").strip(), (e.get("removedTicker") or "").strip()
        if a and a != r:
            members.discard(a)
        if r:
            members.add(r)
    out: dict[str, Member] = {}

    def get(t):
        return out.setdefault(t, Member(t))

    for t in members:
        get(t).spans.append([since, ""])
    for e in events:
        if e["date"] < since:
            continue
        d = e["date"]
        a, r = (e.get("symbol") or "").strip(), (e.get("removedTicker") or "").strip()
        if r:
            m = get(r)
            if e.get("removedSecurity"):
                m.names.add(e["removedSecurity"].strip())
            if m.spans and not m.spans[-1][1]:
                m.spans[-1][1] = d
        if a and a != r:
            m = get(a)
            if e.get("addedSecurity"):
                m.names.add(e["addedSecurity"].strip())
            if not (m.spans and not m.spans[-1][1]):
                m.spans.append([d, ""])
    for t, name in current.items():
        if name:
            get(t).names.add(name)
    return {t: m for t, m in out.items() if m.spans}


# ----------------------------------------------------------------------------------------------
# Prices

def db_ranges(db_path) -> dict[str, tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {s: (a, b) for s, a, b in conn.execute("SELECT symbol, min(date), max(date) FROM prices GROUP BY symbol")}
    finally:
        conn.close()


def db_names(db_path) -> dict[str, tuple[str, str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {s: (n or "", sec or "", ind or "") for s, n, sec, ind in
                conn.execute("SELECT symbol, name, sector, industry FROM symbols")}
    finally:
        conn.close()


def db_bars(db_path, symbol: str, since: str = "2002-01-01") -> dict[str, tuple]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {d: (o, h, lo, c, v) for d, o, h, lo, c, v in conn.execute(
            "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ? AND date >= ? ORDER BY date",
            (symbol, since))}
    finally:
        conn.close()


def sessions_in(member: Member, calendar: list[str], through: str) -> list[str]:
    out = []
    for a, b in member.spans:
        lo = bisect_left(calendar, a)
        hi = bisect_left(calendar, b or "9999") if b else bisect_left(calendar, through + "~")
        out.extend(calendar[lo:hi])
    return out


def fetch_fmp_prices(client, ticker: str) -> dict:
    bars, errors = {}, []
    for a, b in (("2002-01-01", "2013-12-31"), ("2014-01-01", date.today().isoformat())):
        try:
            rows = client.get("historical-price-eod/full", {"symbol": ticker, "from": a, "to": b})
        except FMPAuthError:
            raise
        except FMPError as exc:
            errors.append(str(exc))
            continue
        for r in rows or []:
            if r.get("date") and r.get("close"):
                bars[r["date"]] = (r.get("open"), r.get("high"), r.get("low"), r["close"], r.get("volume"))
    try:
        prof = client.get("profile", {"symbol": ticker})
        prof = prof[0] if isinstance(prof, list) and prof else {}
    except FMPError:
        prof = {}
    return {"fetched_on": date.today().isoformat(), "bars": bars, "errors": errors,
            "name": prof.get("companyName"), "sector": prof.get("sector"), "industry": prof.get("industry")}


# Same company despite a changed name (checked by hand against the change log).
ALIASES = {"IBM", "SLB", "ATI", "CNX", "WAB", "ODP", "ADS", "FNMA", "FMCC"}
# Tickers whose history FMP keeps under a later ticker (checked by hand; an automatic name search matched
# mostly unrelated companies).
RENAMES = {"DPS": "KDP", "DWDP": "DD", "GPS": "GAP", "HFC": "DINO", "UA": "UAA"}
# Words too common to identify a company by name.
GENERIC = {"energy", "financial", "bank", "bancorp", "banks", "resources", "technologies", "technology", "systems",
           "services", "communications", "pharmaceuticals", "industries", "solutions", "global", "america",
           "american", "national", "united", "first", "capital", "health", "healthcare", "partners", "brands",
           "entertainment", "media", "networks", "products", "enterprises", "properties", "realty", "investment",
           "management", "us", "usa", "north", "south", "west", "east", "general", "data", "software", "electric",
           "power", "gas", "oil", "petroleum", "chemical", "foods", "food", "stores", "airlines", "air", "motors",
           "insurance", "life", "materials", "mining", "steel", "therapeutics", "sciences", "medical",
           "laboratories", "worldwide", "companies", "manufacturing", "exploration", "semiconductor", "electronics",
           "instruments", "pharma", "biosciences", "markets", "exchange", "real", "estate", "hotels", "resorts",
           "restaurants", "retail", "drilling", "offshore", "utilities", "water", "devices", "equipment",
           "dynamics", "scientific", "industrial", "consumer", "network", "digital", "labs", "brothers"}


def distinctive(name: str | None) -> frozenset[str]:
    return frozenset(t for t in tokens(name) if t not in GENERIC)


def coverage(bars, days) -> float:
    return sum(d in bars for d in days) / len(days) if days else 0.0


def resolve(ctx, members: dict[str, Member], calendar: list[str]) -> dict:
    """Price series for each member's time in the index, one span at a time.

    Candidates, in order: the ticker in the app database; the ticker fetched from FMP; the later ticker from
    RENAMES, provided it is not in the index itself during the span. A candidate needs bars on 90% of the span's
    sessions and, for the ticker itself, a matching company name (or an alias). Returns {"series": {symbol: {...}}, "missing": [...], "renamed": [...]}.
    """
    db_path = ctx.settings.data.db_path
    ranges, names = db_ranges(db_path), db_names(db_path)
    through = calendar[-1]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        funds = {s for (s,) in conn.execute("SELECT symbol FROM symbols WHERE is_etf = 1 OR is_fund = 1")}
    finally:
        conn.close()
    by_word: dict[str, set] = {}
    for sym, (nm, _, _) in names.items():
        if sym in ranges and sym not in funds:
            for w in distinctive(nm):
                by_word.setdefault(w, set()).add(sym)
    member_days = {t: set(sessions_in(m, calendar, through)) for t, m in members.items()}

    todo = [t for t in members if not ctx.store.document(PX_KEY + t)
            and not (t in ranges and ranges[t][0] <= min(a for a, _ in members[t].spans))]
    if todo:
        print(f"fetching FMP prices for {len(todo)} tickers", flush=True)
        with ThreadPoolExecutor(ctx.settings.fmp.max_workers) as pool:
            futs = {pool.submit(fetch_fmp_prices, ctx.client, t): t for t in todo}
            for f in as_completed(futs):
                ctx.store.save_document(PX_KEY + futs[f], f.result())

    cache: dict[tuple, dict] = {}

    def series_of(sym, src):
        if (sym, src) not in cache:
            cache[sym, src] = (db_bars(db_path, sym, "2003-01-01") if src == "db"
                               else (ctx.store.document(PX_KEY + sym) or {}).get("bars", {}))
        return cache[sym, src]

    series, missing, renamed = {}, [], []
    for t, m in sorted(members.items()):
        for a, b in m.spans:
            days = sessions_in(Member(t, m.names, [[a, b]]), calendar, through)
            if not days:
                continue
            day_set = set(days)
            cands = []
            if t in ranges:
                cands.append((t, "db", names.get(t, ("", "", ""))[0]))
            doc = ctx.store.document(PX_KEY + t)
            if doc:
                cands.append((t, "fmp", doc.get("name")))
            sym = RENAMES.get(t)
            if sym and sym in ranges and not (sym in members and member_days[sym] & day_set):
                cands.append((sym, "db", names[sym][0]))
            chosen, tried = None, []
            for sym, src, nm in cands:
                # Only the part of the span where the series has bars counts: a company that joined under a ticker
                # whose earlier holder FMP lacks keeps its own years.
                have = [d in series_of(sym, src) for d in days]
                if not any(have):
                    tried.append(f"{sym}/{src} no bars")
                    continue
                k0, k1 = have.index(True), len(have) - 1 - have[::-1].index(True)
                cov = sum(have[k0:k1 + 1]) / (k1 + 1 - k0)
                name_ok = (sym != t or sym in ALIASES or not m.names or any(same_company(n, nm) for n in m.names))
                tried.append(f"{sym}/{src} {cov:.0%} of {days[k0]}..{days[k1]} name {'ok' if name_ok else 'no'} ({nm})")
                if cov >= MIN_COVERAGE and name_ok and k1 + 1 - k0 >= min(120, len(days)):
                    if not chosen or k1 - k0 > chosen[4] - chosen[3]:      # the candidate covering the most
                        chosen = (sym, src, nm, k0, k1)
            if not chosen:
                missing.append({"ticker": t, "span": [a, b], "sessions": len(days), "names": sorted(m.names),
                                "tried": tried[:6]})
                continue
            sym, src, nm, k0, k1 = chosen
            if k0 > 0 or k1 < len(days) - 1:
                missing.append({"ticker": t, "span": [a, b], "sessions": len(days) - (k1 + 1 - k0),
                                "names": sorted(m.names), "tried": [f"partly covered by {sym}: {days[k0]}..{days[k1]}"]})
                a = days[k0] if k0 > 0 else a
                if k1 < len(days) - 1:
                    b = calendar[bisect_left(calendar, days[k1]) + 1]
            row = series.setdefault(sym, {"source": src, "name": nm or "", "spans": [], "tickers": []})
            if row["source"] != src:
                missing.append({"ticker": t, "span": [a, b], "sessions": len(days), "names": sorted(m.names),
                                "tried": [f"{sym} already taken from {row['source']}"]})
                continue
            if sym != t:
                renamed.append({"ticker": t, "span": [a, b], "symbol": sym, "name": nm, "names": sorted(m.names)})
            row["spans"].append([a, b])
            if t not in row["tickers"]:
                row["tickers"].append(t)
    for sym, row in series.items():
        if row["source"] == "db":
            _, sector, industry = names.get(sym, ("", "", ""))
        else:
            doc = ctx.store.document(PX_KEY + sym) or {}
            sector, industry = doc.get("sector") or "", doc.get("industry") or ""
        row["sector"], row["industry"] = sector, industry
        row["spans"].sort()
    return {"series": series, "missing": missing, "renamed": renamed}


# FMP-style sectors for delisted members whose profile FMP no longer serves.
SECTOR_FIXES = {"AGN": "Healthcare", "AKS": "Basic Materials", "ANDV": "Energy", "AVP": "Consumer Defensive",
                "BMS": "Consumer Cyclical", "CCE": "Consumer Defensive", "CELG": "Healthcare",
                "CTB": "Consumer Cyclical", "CTX": "Consumer Cyclical", "CXO": "Energy",
                "DTV": "Communication Services", "ETFC": "Financial Services", "FLIR": "Technology",
                "FTR": "Communication Services", "HCR": "Healthcare", "JCP": "Consumer Cyclical",
                "LM": "Financial Services", "QEP": "Energy", "RTN": "Industrials", "TIF": "Consumer Cyclical",
                "TSG": "Technology", "TSS": "Technology", "VAR": "Healthcare", "VIAB": "Communication Services",
                "WCG": "Healthcare", "WPX": "Energy", "XEC": "Energy"}


def fill_sectors(ctx, series: dict) -> None:
    """Sector from the FMP profile where the app database has none, then the hand list."""
    for sym, row in series.items():
        if row.get("sector"):
            continue
        try:
            prof = ctx.client.get("profile", {"symbol": sym}, cache_ttl=timedelta(days=365))
            prof = prof[0] if isinstance(prof, list) and prof else {}
        except FMPError:
            prof = {}
        row["sector"] = prof.get("sector") or SECTOR_FIXES.get(sym, "")
        row["industry"] = row.get("industry") or prof.get("industry") or ""


def bars_for(ctx, ticker: str, source: str) -> dict[str, tuple]:
    if source == "db":
        return db_bars(ctx.settings.data.db_path, ticker)
    return {d: tuple(v) for d, v in (ctx.store.document(PX_KEY + ticker) or {}).get("bars", {}).items()}


def splits_for(ctx, ticker: str) -> list[dict]:
    doc = ctx.store.document(APP_SPLIT_KEY + ticker)
    if doc and not doc.get("error"):
        return doc.get("rows", [])
    doc = ctx.store.document(SPLIT_KEY + ticker)
    if doc is None:
        try:
            rows = ctx.client.splits(ticker, cache_ttl=None)
            doc = {"fetched_on": date.today().isoformat(), "rows": rows or []}
        except FMPAuthError:
            raise
        except FMPError as exc:
            doc = {"fetched_on": date.today().isoformat(), "rows": [], "error": str(exc)}
        ctx.store.save_document(SPLIT_KEY + ticker, doc)
    return doc.get("rows", [])


# ----------------------------------------------------------------------------------------------
# Statements and dividends

def fetch_statements(client, symbol: str) -> dict:
    doc = {"fetched_on": date.today().isoformat()}
    for kind, (path, period, limit) in CALLS.items():
        try:
            rows = client.get(path, {"symbol": symbol, "period": period, "limit": limit})
        except FMPAuthError:
            raise
        except FMPError as exc:
            doc.setdefault("errors", {})[kind] = str(exc)
            rows = []
        doc[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in rows or [] if isinstance(r, dict)]
    return doc


def fetch_dividends(client, symbol: str) -> dict:
    try:
        rows = client.get("dividends", {"symbol": symbol})
        return {"rows": {r["date"]: float(r.get("adjDividend") or 0) for r in rows or [] if r.get("date")}}
    except FMPAuthError:
        raise
    except FMPError as exc:
        return {"rows": {}, "error": str(exc)}


def fetch_all(ctx, symbols: list[str]) -> None:
    def stale(s):
        doc = ctx.store.document(STMT_KEY + s)
        return not doc or any("interestExpense" not in r for r in doc.get("income_annual", [])[:1])
    jobs = [(STMT_KEY + s, fetch_statements, s) for s in symbols if stale(s)]
    jobs += [(DIV_KEY + s, fetch_dividends, s) for s in symbols if not ctx.store.document(DIV_KEY + s)]
    print(f"fetching {len(jobs)} statement/dividend documents", flush=True)
    done = 0
    with ThreadPoolExecutor(ctx.settings.fmp.max_workers) as pool:
        futs = {pool.submit(fn, ctx.client, s): key for key, fn, s in jobs}
        for f in as_completed(futs):
            ctx.store.save_document(futs[f], f.result())
            done += 1
            if done % 500 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}  calls {ctx.client.stats.api_calls}", flush=True)
    for s in symbols:
        splits_for(ctx, s)


def calendar_from_db(db_path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [d for (d,) in conn.execute("SELECT date FROM prices WHERE symbol = 'SPY' AND date >= ? ORDER BY date",
                                            (SINCE,))]
    finally:
        conn.close()


def main(refresh_members: bool = False) -> None:
    ctx = open_context()
    try:
        doc = load_members(ctx, refresh_members)
        members = membership(doc)
        calendar = calendar_from_db(ctx.settings.data.db_path)
        res = resolve(ctx, members, calendar)
        fill_sectors(ctx, res["series"])
        ctx.store.save_document(RESOLVED_KEY, {"made_on": date.today().isoformat(), **res})
        ok = sorted(res["series"])
        print(f"price series {len(ok)}; renamed spans {len(res['renamed'])}; missing spans {len(res['missing'])}")
        fetch_all(ctx, ok)
        print("FMP calls this run:", ctx.client.stats.api_calls)
    finally:
        ctx.close()


if __name__ == "__main__":
    main("--refresh-members" in sys.argv)
