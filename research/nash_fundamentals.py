"""Quarterly income, balance-sheet and cash-flow statements for the Nash quality screen.

Fetches 60 quarters (back to about 2011) for every non-financial stock that passes the
liquidity rule on at least one quarterly rebalance date, keeps only the fields the
screen reads, and caches them in the app database as research:nash:stmts:<symbol>.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import numpy as np

import panel as P
from stratlib.app import open_context
from stratlib.fmp.errors import FMPAuthError, FMPError

KEY = "research:nash:stmts:"
QUARTERS = 60
FIELDS = {
    "income": ("date", "period", "fiscalYear", "filingDate", "reportedCurrency", "revenue", "grossProfit",
               "operatingIncome", "operatingExpenses", "netIncome"),
    "balance": ("date", "filingDate", "cashAndShortTermInvestments", "totalDebt", "capitalLeaseObligations"),
    "cash": ("date", "filingDate", "operatingCashFlow", "capitalExpenditure", "freeCashFlow"),
}
CALLS = {"income": "income_statement", "balance": "balance_sheet", "cash": "cash_flow"}
# Sectors for the few symbols with neither a universe row nor a cached profile.
SECTOR_FIXES = {"BPR": ("Real Estate", "REIT - Retail"), "CELG": ("Healthcare", "Biotechnology"),
                "CLDR": ("Technology", "Software - Infrastructure"), "GRA": ("Basic Materials", "Chemicals - Specialty"),
                "LDL": ("Industrials", "Industrial - Machinery"), "RPAI": ("Real Estate", "REIT - Retail"),
                "XEC": ("Energy", "Oil & Gas Exploration & Production")}


def rebalance_rows(p) -> list[int]:
    """First session of each calendar quarter from 2016."""
    return [i for i, d in enumerate(p.dates)
            if d >= P.STUDY_FROM and d[5:7] in ("01", "04", "07", "10") and p.dates[i - 1][:7] != d[:7]]


def classifications(db_path) -> dict[str, tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        out = {s: (sec or "", ind or "") for s, sec, ind in conn.execute("SELECT symbol, sector, industry FROM symbols")}
        for key, body in conn.execute("SELECT key, body FROM screening_data WHERE key LIKE 'backtest:approx:profile:%'"):
            s, prof = key.rsplit(":", 1)[1], json.loads(body).get("profile")
            if prof and not out.get(s, ("", ""))[0]:
                out[s] = (prof.get("sector") or "", prof.get("industry") or "")
    finally:
        conn.close()
    out.update(SECTOR_FIXES)
    return out


def universe(p, classes) -> list[str]:
    rows = rebalance_rows(p)
    stock = np.flatnonzero(p.kind == "stock")
    ever = stock[p.eligible[rows][:, stock].any(axis=0)]
    return sorted(str(p.symbols[j]) for j in ever if classes.get(str(p.symbols[j]), ("", ""))[0] != "Financial Services")


def fetch_one(client, symbol: str) -> dict:
    doc = {"fetched_on": date.today().isoformat()}
    try:
        for kind, method in CALLS.items():
            rows = getattr(client, method)(symbol, "quarter", QUARTERS, cache_ttl=None)
            if not isinstance(rows, list):
                raise FMPError(f"unexpected {kind} response")
            doc[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in rows if isinstance(r, dict)]
    except FMPAuthError:
        raise
    except FMPError as exc:
        doc = {"fetched_on": doc["fetched_on"], "error": str(exc)}
    return doc


def main(refresh: bool = False) -> None:
    p = P.load()
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        symbols = universe(p, classes)
        todo = [s for s in symbols if refresh or not ctx.store.document(KEY + s)]
        print(f"{len(symbols):,} non-financial stocks; fetching {len(todo):,}")
        done = errors = 0
        with ThreadPoolExecutor(ctx.settings.fmp.max_workers) as pool:
            futures = {pool.submit(fetch_one, ctx.client, s): s for s in todo}
            for f in as_completed(futures):
                doc = f.result()
                ctx.store.save_document(KEY + futures[f], doc)
                done += 1
                errors += "error" in doc
                if done % 250 == 0 or done == len(todo):
                    print(f"  {done:,}/{len(todo):,}  errors {errors}  calls {ctx.client.stats.api_calls:,}", flush=True)
    finally:
        ctx.close()


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
