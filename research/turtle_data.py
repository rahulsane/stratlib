"""Data for the Turtle backtest (turtle_sim.py, run_turtle.py).

Six ETFs as stand-ins for the Turtles' futures: SPY, QQQ, GLD (gold), SLV (silver), USO (crude oil) and
TLT (20+ year Treasuries, the liquid proxy for the 30-year bond).

- Bars: FMP dividend-adjusted daily OHLC (historical-price-eod/dividend-adjusted). Trading on these makes a
  long position earn the distributions and a short pay them, as a futures price would carry them.
- As-traded closes: FMP split-adjusted closes (historical-price-eod/full) times the splits after each day,
  for the harness's $20 cost tier (SLV split 10:1 in 2008, USO 1:8 in 2020).
- Cash rate: FMP 3-month Treasury yields (treasury-rates, about 90 days per call).

Cached in research/cache/turtle_etfs.json. Run with --refresh to fetch through today:
    PYTHONPATH=src .venv/Scripts/python research/turtle_data.py --refresh
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SYMBOLS = ("SPY", "QQQ", "GLD", "SLV", "USO", "TLT")
FROM = date(2004, 1, 1)
CHUNKS = ((date(2004, 1, 1), date(2015, 12, 31)), (date(2016, 1, 1), None))
RATE_CHUNK_DAYS = 85
CACHE = Path(__file__).resolve().parent / "cache" / "turtle_etfs.json"


def refresh() -> dict:
    from stratlib.app import open_context

    ctx = open_context()
    client = ctx.client
    out = {"fetched_on": date.today().isoformat(), "bars": {}, "splits": {}, "tbill3m": {}}
    try:
        for sym in SYMBOLS:
            adj, full = {}, {}
            for a, b in CHUNKS:
                b = b or date.today()
                for r in client.get("historical-price-eod/dividend-adjusted", {"symbol": sym, "from": a, "to": b}) or []:
                    adj[r["date"]] = r
                for r in client.get("historical-price-eod/full", {"symbol": sym, "from": a, "to": b}) or []:
                    full[r["date"]] = r
            days = sorted(d for d in adj if d in full)
            out["bars"][sym] = {d: [adj[d]["adjOpen"], adj[d]["adjHigh"], adj[d]["adjLow"], adj[d]["adjClose"],
                                    full[d]["close"], full[d].get("volume") or 0] for d in days}
            out["splits"][sym] = [(r["date"], r["numerator"], r["denominator"])
                                  for r in client.get("splits", {"symbol": sym}) or []]
        day = FROM
        while day <= date.today():
            end = min(day + timedelta(days=RATE_CHUNK_DAYS - 1), date.today())
            for r in client.get("treasury-rates", {"from": day, "to": end}) or []:
                if r.get("date") and r.get("month3") is not None:
                    out["tbill3m"][r["date"]] = float(r["month3"])
            day = end + timedelta(days=1)
    finally:
        ctx.close()
    CACHE.write_text(json.dumps(out))
    return {s: len(v) for s, v in out["bars"].items()} | {"tbill_days": len(out["tbill3m"])}


@dataclass
class Data:
    symbols: tuple[str, ...]
    days: np.ndarray          # ISO dates, sessions where every symbol has a bar
    open: np.ndarray          # (days, symbols), dividend-adjusted
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    traded: np.ndarray        # as-traded close (split basis of that day, no dividend adjustment)
    rate: np.ndarray          # 3-month T-bill yield in % on or before each session
    fetched_on: str


def load(symbols: tuple[str, ...] = SYMBOLS) -> Data:
    raw = json.loads(CACHE.read_text())
    # A bar dated the fetch day may be a partial session; keep completed sessions only.
    days = sorted(d for d in set.intersection(*(set(raw["bars"][s]) for s in symbols)) if d < raw["fetched_on"])
    n, m = len(days), len(symbols)
    arr = np.zeros((6, n, m))
    for j, s in enumerate(symbols):
        bars = raw["bars"][s]
        for i, d in enumerate(days):
            arr[:, i, j] = bars[d]
        for sd, num, den in raw["splits"][s]:
            later = np.array(days) < sd          # sessions before the split traded at num/den times the adjusted price
            arr[4, later, j] *= num / den
    o, h, lo, c = arr[0], arr[1], arr[2], arr[3]
    # FMP occasionally reports a high below the open/close or a low above them; clip to the bar's body.
    h = np.maximum(h, np.maximum(o, c))
    lo = np.minimum(lo, np.minimum(o, c))
    rdays = sorted(raw["tbill3m"])
    rate, k = np.zeros(n), 0
    for i, d in enumerate(days):
        while k + 1 < len(rdays) and rdays[k + 1] <= d:
            k += 1
        rate[i] = raw["tbill3m"][rdays[k]] if rdays[k] <= d else raw["tbill3m"][rdays[0]]
    return Data(tuple(symbols), np.array(days), o, h, lo, c, arr[4], rate, raw["fetched_on"])


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print(refresh())
    d = load()
    print(f"{len(d.days)} common sessions {d.days[0]}..{d.days[-1]}; T-bill latest {d.rate[-1]}%")
    for j, s in enumerate(d.symbols):
        print(f"  {s}: adj close {d.close[0, j]:.2f} -> {d.close[-1, j]:.2f}; as traded {d.traded[0, j]:.2f} -> {d.traded[-1, j]:.2f}")
