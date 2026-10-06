"""Statements and valuation history for the Nash rule study (deeper and wider than nash_fundamentals.py).

80 quarters (back to about 2006) of income, balance-sheet and cash-flow statements plus FMP key-metrics
(quarter-end market cap and enterprise value, in the reporting currency) for every non-financial stock that
passes the liquidity rule on a quarterly rebalance date in the holdout panel (2011-2015) or the main panel
(2016 on). Cached as research:nash2:stmts:<symbol>.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import numpy as np

import nash_panels
import panel as P
from stratlib.app import open_context
from stratlib.fmp.errors import FMPAuthError, FMPError
from nash_fundamentals import classifications

KEY = "research:nash2:stmts:"
QUARTERS = 80
FIELDS = {
    "income": ("date", "period", "fiscalYear", "filingDate", "reportedCurrency", "revenue", "grossProfit",
               "operatingIncome", "operatingExpenses", "netIncome", "incomeBeforeTax", "incomeTaxExpense",
               "weightedAverageShsOutDil"),
    "balance": ("date", "filingDate", "reportedCurrency", "cashAndShortTermInvestments", "totalDebt",
                "capitalLeaseObligations", "totalStockholdersEquity", "totalAssets"),
    "cash": ("date", "filingDate", "reportedCurrency", "operatingCashFlow", "capitalExpenditure", "freeCashFlow",
             "stockBasedCompensation"),
    "metrics": ("date", "reportedCurrency", "marketCap", "enterpriseValue"),
}


def universe_symbols(classes) -> tuple[list[str], list[str]]:
    """Non-financial stocks eligible on some rebalance date; also those whose sector is unknown."""
    wanted = set()
    for p, start in ((nash_panels.load_holdout(), "2011-01-01"), (P.load(), "2016-01-01")):
        rows = nash_panels.rebalance_rows(p, start)
        stock = np.flatnonzero(p.kind == "stock")
        wanted |= {str(p.symbols[j]) for j in stock[p.eligible[rows][:, stock].any(axis=0)]}
    unknown = sorted(s for s in wanted if not classes.get(s, ("", ""))[0])
    return sorted(s for s in wanted if classes.get(s, ("", ""))[0] not in ("Financial Services",)), unknown


def fetch_one(client, symbol: str) -> dict:
    doc = {"fetched_on": date.today().isoformat()}
    try:
        for kind, (path, params) in {
            "income": ("income-statement", {}), "balance": ("balance-sheet-statement", {}),
            "cash": ("cash-flow-statement", {}), "metrics": ("key-metrics", {}),
        }.items():
            rows = client.get(path, {"symbol": symbol, "period": "quarter", "limit": QUARTERS, **params})
            if not isinstance(rows, list):
                raise FMPError(f"unexpected {kind} response")
            doc[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in rows if isinstance(r, dict)]
    except FMPAuthError:
        raise
    except FMPError as exc:
        doc = {"fetched_on": doc["fetched_on"], "error": str(exc)}
    return doc


def main() -> None:
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        symbols, unknown = universe_symbols(classes)
        print(f"{len(symbols):,} stocks (sector unknown for {len(unknown)}: {unknown[:20]})")
        todo = [s for s in symbols if not ctx.store.document(KEY + s)]
        print(f"fetching {len(todo):,}")
        done = errors = 0
        with ThreadPoolExecutor(ctx.settings.fmp.max_workers) as pool:
            futures = {pool.submit(fetch_one, ctx.client, s): s for s in todo}
            for f in as_completed(futures):
                doc = f.result()
                ctx.store.save_document(KEY + futures[f], doc)
                done += 1
                errors += "error" in doc
                if done % 500 == 0 or done == len(todo):
                    print(f"  {done:,}/{len(todo):,}  errors {errors}  calls {ctx.client.stats.api_calls:,}", flush=True)
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
