"""SPY and QQQ from their first trading days, for the MA pullback on the two index ETFs (run_ma_pullback_etf.py).

- Bars: FMP split-adjusted daily OHLC and volume (historical-price-eod/full), SPY from 1993-01-29 and QQQ from
  1999-03-10. Highs and lows outside the bar's body are clipped to it.
- As-traded prices (for the $20 cost tier): the adjusted price times the splits after each day (QQQ 2:1 in 2000).
- Dividends: FMP split-adjusted dividends by ex-date, credited by the tax accountant like the stock studies.
- 3-month T-bill yields from 1990 (treasury-rates), for the margin rate in the leverage check.
Cached in research/cache/ma_pullback_etfs.json. Refresh:
    PYTHONPATH="src;research" .venv/Scripts/python.exe research/ma_pullback_etf_data.py --refresh
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from stratlib.sim.panel import Panel

SYMBOLS = ("SPY", "QQQ")
FIRST = {"SPY": date(1993, 1, 1), "QQQ": date(1999, 3, 1)}
THROUGH = "2026-09-30"
RATES_FROM = date(1990, 1, 1)
CHUNK_YEARS = 8
RATE_CHUNK_DAYS = 85
CACHE = Path(__file__).resolve().parent / "cache" / "ma_pullback_etfs.json"


def refresh() -> dict:
    from stratlib.app import open_context

    ctx = open_context()
    client = ctx.client
    out = {"fetched_on": date.today().isoformat(), "bars": {}, "splits": {}, "dividends": {}, "tbill3m": {}}
    end = date.fromisoformat(THROUGH)
    try:
        for sym in SYMBOLS:
            bars, a = {}, FIRST[sym]
            while a <= end:
                b = min(date(a.year + CHUNK_YEARS, 1, 1) - timedelta(days=1), end)
                for r in client.get("historical-price-eod/full", {"symbol": sym, "from": a, "to": b}) or []:
                    bars[r["date"]] = [r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0]
                a = b + timedelta(days=1)
            out["bars"][sym] = dict(sorted(bars.items()))
            out["splits"][sym] = [(r["date"], r["numerator"], r["denominator"])
                                  for r in client.get("splits", {"symbol": sym}) or []]
            out["dividends"][sym] = {r["date"]: float(r.get("adjDividend") or r.get("dividend") or 0)
                                     for r in client.get("dividends", {"symbol": sym}) or [] if r.get("date")}
        day = RATES_FROM
        while day <= end:
            stop = min(day + timedelta(days=RATE_CHUNK_DAYS - 1), end)
            for r in client.get("treasury-rates", {"from": day, "to": stop}) or []:
                if r.get("date") and r.get("month3") is not None:
                    out["tbill3m"][r["date"]] = float(r["month3"])
            day = stop + timedelta(days=1)
    finally:
        ctx.close()
    CACHE.write_text(json.dumps(out))
    return {s: len(v) for s, v in out["bars"].items()} | {"tbill_days": len(out["tbill3m"]),
                                                          "dividends": {s: len(v) for s, v in out["dividends"].items()}}


def load() -> dict:
    """{"panel": Panel (SPY sessions, columns SPY and QQQ), "dividends": {session: [(column, per share)]},
    "spy_dividends": {date: per share}, "qqq_dividends": {...}, "tbill3m": {date: %}}."""
    raw = json.loads(CACHE.read_text())
    days = [d for d in raw["bars"]["SPY"] if d <= THROUGH]
    n, m = len(days), len(SYMBOLS)
    arr = np.full((5, n, m), np.nan)
    factor = np.ones((n, m))
    darr = np.array(days)
    for j, s in enumerate(SYMBOLS):
        bars = raw["bars"][s]
        for i, d in enumerate(days):
            if d in bars:
                arr[:, i, j] = bars[d]
        for sd, num, den in raw["splits"][s]:
            factor[darr < sd, j] *= num / den
    o, h, lo, c, v = arr
    with np.errstate(invalid="ignore"):
        h = np.fmax(h, np.fmax(o, c))
        lo = np.fmin(lo, np.fmin(o, c))
    panel = Panel(dates=darr, symbols=np.array(SYMBOLS), kind=np.array(["etf"] * m), until=np.array([""] * m),
                  open=o, high=h, low=lo, close=c, volume=v, factor=factor, min_price=1.0, min_dollar_volume=0.0,
                  notes={"source": "split-adjusted daily bars from the first trading day"})
    events: dict[int, list] = {}
    for j, s in enumerate(SYMBOLS):
        for ex, amount in raw["dividends"][s].items():
            t = int(np.searchsorted(darr, ex))
            if amount > 0 and 0 < t < n:
                events.setdefault(t, []).append((j, amount))
    return {"panel": panel, "dividends": events, "spy_dividends": raw["dividends"]["SPY"],
            "qqq_dividends": raw["dividends"]["QQQ"], "tbill3m": raw["tbill3m"], "fetched_on": raw["fetched_on"]}


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print(refresh())
    d = load()
    p = d["panel"]
    print(f"{len(p.dates)} sessions {p.dates[0]}..{p.dates[-1]}")
    for j, s in enumerate(SYMBOLS):
        first = int(np.argmax(np.isfinite(p.close[:, j])))
        print(f"  {s}: from {p.dates[first]}, close {p.close[first, j]:.2f} -> {p.close[-1, j]:.2f}, "
              f"as traded {p.as_traded_close[first, j]:.2f}; missing bars after start "
              f"{int((~np.isfinite(p.close[first:, j])).sum())}; dividends {len(d[s.lower() + '_dividends'])}")
    print("T-bills", min(d["tbill3m"]), max(d["tbill3m"]), len(d["tbill3m"]))
