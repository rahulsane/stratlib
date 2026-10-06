"""SPY dividends (for the total-return benchmark) and 3-month T-bill yields (for Sharpe).

Cached in the app database as research:spy_dividends and research:tbill3m.
Run with --refresh to fetch through today.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from stratlib.app import open_context

FROM = date(2015, 12, 1)
CHUNK_DAYS = 85  # FMP returns about 90 days of treasury rates per call


def refresh() -> dict:
    ctx = open_context()
    client, store = ctx.client, ctx.store
    try:
        rows = client.get("dividends", {"symbol": "SPY"}, cache_ttl=timedelta(hours=12))
        dividends = {r["date"]: float(r.get("adjDividend") or r.get("dividend") or 0) for r in rows if r.get("date")}
        store.save_document("research:spy_dividends", {"fetched_on": date.today().isoformat(), "rows": dividends})

        rates, day = {}, FROM
        while day <= date.today():
            end = min(day + timedelta(days=CHUNK_DAYS - 1), date.today())
            for r in client.get("treasury-rates", {"from": day, "to": end}, cache_ttl=timedelta(hours=12)) or []:
                if r.get("date") and r.get("month3") is not None:
                    rates[r["date"]] = float(r["month3"])
            day = end + timedelta(days=1)
        store.save_document("research:tbill3m", {"fetched_on": date.today().isoformat(), "rows": rates})
        return {"dividends": len(dividends), "tbill_days": len(rates), "api_calls": client.stats.api_calls}
    finally:
        ctx.close()


def load() -> dict:
    ctx = open_context()
    try:
        div = ctx.store.document("research:spy_dividends")
        bill = ctx.store.document("research:tbill3m")
    finally:
        ctx.close()
    if not div or not bill:
        raise RuntimeError("Run research/benchmarks.py --refresh first.")
    return {"spy_dividends": div["rows"], "tbill3m": bill["rows"]}


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print(refresh())
    b = load()
    d, t = sorted(b["spy_dividends"]), sorted(b["tbill3m"])
    print(f"SPY dividends: {len(d)} ({d[0]}..{d[-1]}); T-bill days: {len(t)} ({t[0]}..{t[-1]}), latest {b['tbill3m'][t[-1]]}%")
