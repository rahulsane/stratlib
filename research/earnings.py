"""Earnings release dates per stock (FMP `earnings?symbol=`), cached in the app database.

FMP Premium refuses earnings-calendar dates older than about five years, so the per-symbol history
is used instead (back to 2002 for long-listed stocks). It has no time of day: a release dated D can
be before the open (first session after it: D) or after the close (the next session). Tickers merged
as renames of the same company (panel.py) contribute their dates to the ticker that was kept.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from stratlib.app import open_context
from stratlib.fmp import FMPAuthError, FMPError
from stratlib.fmp.client import cache_key, clean_params

PATH = "earnings"
LIMIT = 100  # about 25 years of quarters
TTL = timedelta(days=30)


def _key(symbol: str) -> str:
    return cache_key(PATH, clean_params({"symbol": symbol, "limit": LIMIT}))


def tickers(panel) -> dict[str, list[str]]:
    """Kept stock ticker -> every ticker whose earnings apply to it (itself and merged renames)."""
    groups = {str(s): [str(s)] for s, k in zip(panel.symbols, panel.kind) if k == "stock"}
    for d in panel.notes.get("duplicates", []):
        if d["keep"] in groups:
            groups[d["keep"]] += d["merged"]
    return groups


def fetch(panel, workers: int = 8, progress=print) -> dict:
    ctx = open_context()
    errors = {}
    try:
        wanted = sorted({s for group in tickers(panel).values() for s in group})
        todo = [s for s in wanted if ctx.store.get_response(_key(s), TTL) is None]
        progress(f"Earnings histories: {len(wanted):,} tickers, {len(todo):,} to download")
        done = [0]

        def one(symbol):
            try:
                ctx.client.get(PATH, {"symbol": symbol, "limit": LIMIT}, cache_ttl=TTL)
            except FMPAuthError:
                raise
            except FMPError as exc:
                errors[symbol] = str(exc)[:200]
            done[0] += 1
            if done[0] % 1000 == 0:
                progress(f"  {done[0]:,}/{len(todo):,}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        return {"tickers": len(wanted), "downloaded": len(todo) - len(errors), "errors": len(errors),
                "api_calls": ctx.client.stats.api_calls}
    finally:
        ctx.close()


def load(panel) -> dict[str, list[str]]:
    """Kept stock ticker -> sorted release dates (ISO), from the cache only."""
    ctx = open_context()
    try:
        result = {}
        for keep, group in tickers(panel).items():
            dates = set()
            for s in group:
                rows = ctx.store.get_response(_key(s), timedelta(days=36500)) or []
                dates.update(str(r["date"])[:10] for r in rows if r.get("date"))
            result[keep] = sorted(dates)
        return result
    finally:
        ctx.close()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import panel as panel_module

    print(fetch(panel_module.load()))
