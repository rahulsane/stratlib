"""Download ETF price histories (current and delisted) and splits for the research backtests.

Bars go into the app's prices table beside the stocks, on the same
split-adjusted basis. Splits and profiles are fetched only for ETFs whose
20-day average dollar volume ever reached the universe floor since 2016,
because dollar volume does not depend on the split basis.

Run: .venv\\Scripts\\python research\\prepare_etfs.py
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import numpy as np

from stratlib.app import open_context
from stratlib.backfill import fetch_bars
from stratlib.backtest_approx import members
from stratlib.fmp import FMPAuthError, FMPError
from stratlib.prices import eod_cutoff

EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "CBOE")
HISTORY_FROM = date(2007, 1, 1)
STUDY_FROM = "2016-01-01"
FLOOR_DOLLAR_VOLUME = 20e6
# Liquid leveraged and volatility products that closed before 2022 but are
# missing from FMP's delisted directory. FMP has prices for some of them
# (XIV has none). Added only if the ticker is not in use today.
KNOWN_DELISTED = ("TVIX", "TVIZ", "XIV", "ZIV", "VIIX", "UGAZ", "DGAZ", "UWT", "DSLV", "UGLD", "DGLD", "GASX",
                  "RUSL", "SPXU", "JDST", "LABD", "DRIP", "GUSH", "BIS", "ERY", "YANG", "EDZ", "TWM", "SRTY")
ETF_NAME = re.compile(
    r"\bETF\b|\bETN\b|ProShares|Direxion|iShares|SPDR|VelocityShares|iPath|PowerShares|Invesco|WisdomTree"
    r"|Global X|Vanguard|First Trust|VanEck|Market Vectors|Xtrackers|Credit Suisse .*Exchange Traded"
    r"|Barclays .*Bank|UBS AG .*ETRACS|ETRACS|MicroSectors|Leveraged|Inverse|Bull 2X|Bear 2X|Bull 3X|Bear 3X",
    re.I,
)


def log(message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {message}", flush=True)


def parallel(items, work, workers=8, label=""):
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(work, items):
            done += 1
            if done % 500 == 0:
                log(f"{label}: {done:,}/{len(items):,}")


def main() -> int:
    ctx = open_context()
    client, store, settings = ctx.client, ctx.store, ctx.settings
    cutoff = eod_cutoff(datetime.now(timezone.utc), settings.prices.eod_final_hour_et)
    try:
        # 1. Current ETFs.
        current = {}
        for exchange in EXCHANGES:
            for page in range(20):
                rows = client.company_screener(exchange=exchange, is_etf=True, is_fund=None,
                                               is_actively_trading=True, page=page, limit=5000)
                for row in rows:
                    if row.get("isEtf") and row.get("symbol"):
                        current.setdefault(row["symbol"], {"symbol": row["symbol"], "name": row.get("companyName"),
                                                           "exchange": exchange, "until": None})
                if len(rows) < 5000:
                    break
        log(f"Current ETFs: {len(current):,}")

        # 2. Delisted ETFs from FMP's directory. A ticker now used by a listed
        # security or a stock member is skipped: its prices are the new one's.
        stock_members, _ = members(store, settings, STUDY_FROM, cutoff.isoformat())
        listed = set(store.universe_symbols()) | set(current) | set(stock_members)
        with store._lock:
            listed |= {s for (s,) in store._conn.execute("SELECT symbol FROM symbols WHERE listed = 1")}
        directory = (store.document("backtest:delisted") or {}).get("rows", [])
        delisted, reused = {}, 0
        for row in directory:
            symbol, until = row.get("symbol"), row.get("delistedDate") or ""
            if row.get("exchange") not in EXCHANGES or until < STUDY_FROM or not symbol:
                continue
            profile = (store.document("backtest:approx:profile:" + symbol) or {}).get("profile") or {}
            if not (profile.get("isEtf") or ETF_NAME.search(row.get("companyName") or "")):
                continue
            if symbol in listed:
                reused += 1
                continue
            if symbol not in delisted or until > delisted[symbol]["until"]:
                delisted[symbol] = {"symbol": symbol, "name": row.get("companyName"), "exchange": row["exchange"],
                                    "until": until}
        extras = [s for s in KNOWN_DELISTED if s not in listed and s not in delisted]
        for symbol in extras:
            delisted[symbol] = {"symbol": symbol, "name": None, "exchange": None, "until": "pending"}
        log(f"Delisted ETF candidates: {len(delisted):,} (skipped {reused} reused tickers; "
            f"{len(extras)} known products added: {', '.join(extras)})")

        # 3. Prices from 2007, one call per symbol (FMP serves 5,000 rows per call).
        everything = {**delisted, **current}
        states = store.price_states(list(everything))
        todo = [s for s in everything
                if s not in states or states[s].status != "ok" or (states[s].requested_from or "9999") > HISTORY_FROM.isoformat()
                or (everything[s]["until"] is None and (states[s].last_date or "") < cutoff.isoformat())]
        log(f"Price histories to fetch: {len(todo):,}")

        def prices(symbol):
            now = datetime.now(timezone.utc)
            try:
                bars = [b for b in fetch_bars(client, symbol, HISTORY_FROM, cutoff) if b.date <= cutoff.isoformat()]
                store.write_prices(symbol, bars, replace=True, requested_from=HISTORY_FROM.isoformat(),
                                   checked_at=now, status="ok" if bars else "empty")
            except FMPAuthError:
                raise
            except FMPError as exc:
                store.write_prices(symbol, [], replace=False, requested_from=None, checked_at=now,
                                   status="error", error=str(exc)[:300])

        parallel(todo, prices, label="Prices")
        states = store.price_states(list(everything))
        for symbol, item in list(everything.items()):
            if item["until"] == "pending":
                state = states.get(symbol)
                if state and state.status == "ok" and state.last_date:
                    item["until"] = state.last_date
                else:
                    everything.pop(symbol)
                    delisted.pop(symbol, None)

        # 4. Which ETFs ever reached the dollar-volume floor since 2016.
        qualifying = []
        for symbol in everything:
            rows = store.price_rows(symbol, "2015-11-01", cutoff.isoformat())
            if len(rows) < 20:
                continue
            dv = np.array([(c or 0) * (v or 0) for _, _, c, v in rows], dtype=float)
            avg = np.convolve(dv, np.ones(20) / 20, mode="valid")
            days = [r[0] for r in rows][19:]
            if any(a >= FLOOR_DOLLAR_VOLUME and d >= STUDY_FROM for a, d in zip(avg, days)):
                qualifying.append(symbol)
        log(f"ETFs ever at or above $20M average dollar volume since 2016: {len(qualifying):,} "
            f"({sum(1 for s in qualifying if everything[s]['until']):,} delisted)")

        # 5. Split histories for qualifying ETFs, and profiles for delisted ones.
        today = date.today().isoformat()

        def splits(symbol):
            doc = store.document(f"backtest:splits:{symbol}")
            if doc and doc.get("fetched_on") == today and not doc.get("error"):
                return
            try:
                rows = client.splits(symbol, cache_ttl=None)
                store.save_document(f"backtest:splits:{symbol}", {"fetched_on": today, "rows": rows})
            except FMPAuthError:
                raise
            except FMPError:
                store.save_document(f"backtest:splits:{symbol}", {"fetched_on": "", "rows": [], "error": True})

        parallel(qualifying, splits, label="Splits")

        def profile(symbol):
            key = "backtest:approx:profile:" + symbol
            if store.document(key):
                return
            try:
                rows = client.profile(symbol)
                store.save_document(key, {"profile": rows[0] if isinstance(rows, list) and rows else None})
            except FMPAuthError:
                raise
            except FMPError:
                store.save_document(key, {"profile": None})

        parallel([s for s in qualifying if everything[s]["until"]], profile, label="Profiles")

        confirmed = []
        for symbol in qualifying:
            item = everything[symbol]
            if item["until"]:
                prof = (store.document("backtest:approx:profile:" + symbol) or {}).get("profile") or {}
                item["profile_is_etf"] = prof.get("isEtf")
            confirmed.append(item)
        store.save_document("research:etf_universe", {
            "fetched_on": today, "through": cutoff.isoformat(), "current_count": len(current),
            "delisted_candidates": len(delisted), "reused_tickers_skipped": reused,
            "qualifying": confirmed,
        })
        log(f"Saved research:etf_universe with {len(confirmed):,} qualifying ETFs. API calls: {client.stats.api_calls:,}")
    finally:
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
