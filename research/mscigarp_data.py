"""Extra data for the MSCI USA Quality GARP Select replication (mscigarp_index.py, mscigarp_backtest.py).

- Quarterly cash-flow statements (operating cash flow, D&A) and balance-sheet cash for every price series in the
  S&P 500 GARP data (garp_data.py), cached as research:mscigarp:extra:<symbol>. The MSCI value score needs
  EV/CFO; the S&P study did not.
- MSCI's official daily index levels from its public end-of-day service (app2.msci.com), cached as
  research:mscigarp:levels:<code>:<variant>. 756664 = MSCI USA Quality GARP Select, 984000 = MSCI USA.
  Variants: GRTR (gross total return, the fund's benchmark), NETR, STRD (price).

Run: PYTHONPATH=src .venv/Scripts/python research/mscigarp_data.py
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests

import garp_data as GD
from stratlib.app import open_context
from stratlib.fmp.errors import FMPAuthError, FMPError

EXTRA_KEY = "research:mscigarp:extra:"
LEVELS_KEY = "research:mscigarp:levels:"
INDEXES = {"756664": "MSCI USA Quality GARP Select", "984000": "MSCI USA"}
LEVELS_URL = ("https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?currency_symbol=USD"
              "&index_variant={variant}&start_date=19970101&end_date={end}&data_frequency=DAILY&index_codes={code}")
FIELDS = {
    "balance_quarter": ("date", "filingDate", "acceptedDate", "cashAndCashEquivalents", "cashAndShortTermInvestments",
                        "minorityInterest"),
    "cash_quarter": ("date", "filingDate", "acceptedDate", "operatingCashFlow", "depreciationAndAmortization",
                     "netIncome"),
}
CALLS = {"balance_quarter": "balance-sheet-statement", "cash_quarter": "cash-flow-statement"}


def fetch_extra(client, symbol: str) -> dict:
    doc = {"fetched_on": date.today().isoformat()}
    for kind, path in CALLS.items():
        try:
            rows = client.get(path, {"symbol": symbol, "period": "quarter", "limit": 100})
        except FMPAuthError:
            raise
        except FMPError as exc:
            doc.setdefault("errors", {})[kind] = str(exc)
            rows = []
        doc[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in rows or [] if isinstance(r, dict)]
    return doc


def fetch_levels(store, refresh: bool = False) -> None:
    for code in INDEXES:
        for variant in ("GRTR", "NETR", "STRD"):
            key = f"{LEVELS_KEY}{code}:{variant}"
            if store.document(key) and not refresh:
                continue
            url = LEVELS_URL.format(variant=variant, code=code, end=date.today().strftime("%Y%m%d"))
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            resp.raise_for_status()
            rows = resp.json()["indexes"]["INDEX_LEVELS"]
            levels = {f"{r['calc_date'] // 10000:04d}-{r['calc_date'] // 100 % 100:02d}-{r['calc_date'] % 100:02d}":
                      float(r["level_eod"]) for r in rows}
            store.save_document(key, {"fetched_on": date.today().isoformat(), "name": INDEXES[code],
                                      "variant": variant, "levels": levels})
            print(f"{INDEXES[code]} {variant}: {len(levels)} days {min(levels)}..{max(levels)}")


def levels(store, code: str, variant: str = "GRTR") -> dict[str, float]:
    return store.document(f"{LEVELS_KEY}{code}:{variant}")["levels"]


def main(refresh: bool = False) -> None:
    ctx = open_context()
    try:
        fetch_levels(ctx.store, refresh)
        symbols = sorted(ctx.store.document(GD.RESOLVED_KEY)["series"])
        todo = [s for s in symbols if refresh or not ctx.store.document(EXTRA_KEY + s)]
        print(f"fetching cash-flow and cash for {len(todo)} of {len(symbols)} symbols", flush=True)
        done = 0
        with ThreadPoolExecutor(ctx.settings.fmp.max_workers) as pool:
            futs = {pool.submit(fetch_extra, ctx.client, s): s for s in todo}
            for f in as_completed(futs):
                ctx.store.save_document(EXTRA_KEY + futs[f], f.result())
                done += 1
                if done % 250 == 0 or done == len(todo):
                    print(f"  {done}/{len(todo)}  calls {ctx.client.stats.api_calls}", flush=True)
    finally:
        ctx.close()


if __name__ == "__main__":
    main("--refresh" in sys.argv)
