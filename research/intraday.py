"""One-minute bars for breakout days (FMP historical-chart/1min), cached in the app database.

FMP serves roughly the last few years of one-minute bars on the Premium plan. Each
(symbol, day) costs one call the first time and is then read from the cache.
FMP serves some one-minute histories adjusted for later splits and some as
traded; the basis is chosen from the opening price (known at 09:30), either
unchanged or adjusted with the panel's split factor. A day whose one-minute high
or low disagrees with the daily bar by more than 1.5% is treated as missing (a
data-quality check).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from stratlib.app import open_context
from stratlib.fmp import FMPAuthError, FMPError
from stratlib.fmp.client import cache_key, clean_params

PATH = "historical-chart/1min"
FOREVER = timedelta(days=36500)
TOLERANCE = 0.015


def _key(symbol: str, day: str) -> str:
    return cache_key(PATH, clean_params({"symbol": symbol, "from": day, "to": day}))


def fetch(pairs: list[tuple[str, str]], workers: int = 8, progress=print) -> dict:
    """Download (symbol, day) pairs not yet cached. Returns call and error counts."""
    ctx = open_context()
    errors, done = {}, [0]
    try:
        todo = [(s, d) for s, d in pairs if ctx.store.get_response(_key(s, d), FOREVER) is None]
        progress(f"One-minute days: {len(pairs):,} needed, {len(todo):,} to download")

        def one(pair):
            symbol, day = pair
            try:
                ctx.client.get(PATH, {"symbol": symbol, "from": day, "to": day}, cache_ttl=FOREVER)
            except FMPAuthError:
                raise
            except FMPError as exc:
                errors[pair] = str(exc)[:200]
            done[0] += 1
            if done[0] % 500 == 0:
                progress(f"  {done[0]:,}/{len(todo):,}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
        return {"needed": len(pairs), "downloaded": len(todo) - len(errors), "errors": len(errors),
                "api_calls": ctx.client.stats.api_calls}
    finally:
        ctx.close()


@dataclass
class Day:
    minute: np.ndarray  # minutes after 09:30
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


class Intraday:
    """Read cached one-minute days on the panel's price basis (no API calls)."""

    def __init__(self, panel):
        self.panel = panel
        self.ctx = open_context()
        self.memo: dict[tuple[int, int], Day | None] = {}
        self.missing = self.mismatched = 0

    def close(self) -> None:
        self.ctx.close()

    def day(self, j: int, t: int) -> Day | None:
        if (j, t) in self.memo:
            return self.memo[(j, t)]
        p = self.panel
        rows = self.ctx.store.get_response(_key(str(p.symbols[j]), str(p.dates[t])), FOREVER)
        result = None
        if not rows:
            self.missing += 1
        else:
            rows = sorted((r for r in rows if str(r.get("date", "")).startswith(str(p.dates[t]))),
                          key=lambda r: r["date"])
            minute = np.array([(int(r["date"][11:13]) - 9) * 60 + int(r["date"][14:16]) - 30 for r in rows])
            keep = (minute >= 0) & (minute < 390)
            arr = {k: np.array([float(r[k]) for r in rows])[keep] for k in ("open", "high", "low", "close")}
            minute = minute[keep]
            if len(minute) == 0 or not np.isfinite(p.close[t, j]):
                self.missing += 1
            else:
                # FMP serves some one-minute histories adjusted for later splits and some as
                # traded. Pick the basis from the opening price (known at 09:30): unchanged, or
                # adjusted with the panel's split factor, whichever is closer to the daily open.
                implied = p.open[t, j] / arr["open"][0] if np.isfinite(p.open[t, j]) else 1.0
                scale = min((1.0, 1 / p.factor[t, j]), key=lambda c: abs(np.log(implied / c)))
                arr = {k: v * scale for k, v in arr.items()}
                if (abs(arr["high"].max() / p.high[t, j] - 1) > TOLERANCE
                        or abs(arr["low"].min() / p.low[t, j] - 1) > TOLERANCE):
                    self.mismatched += 1
                else:
                    result = Day(minute=minute, **arr)
        self.memo[(j, t)] = result
        return result
