"""Data and model for the leveraged S&P 500 trend study (run_levtrend.py, output/levtrend/report.md).

Rules fixed on 2026-09-29, before any results were seen:
- Index: S&P 500 (^GSPC) daily closes plus Robert Shiller's monthly dividends, spread evenly over each month's
  sessions. Cash: Shiller's annual one-year rate (commercial paper, later CDs) for 1950-1989, 3-month Treasury
  yields from 1990.
- L x exposure, reset daily: L x index total return - (L - 1) x (cash + spread) - fee, accrued by calendar days.
  As registered: spread 0.5%, fee 0.09% a year at 1x and 0.9% above. The funds' own costs, fitted to their actual
  returns, are fund_costs() in run_levtrend.py.
- Trend: the S&P close against its 200-session simple average, checked at each month-end (the main rule) or every
  session; the switch fills at the next session's close and costs 0.1% of equity. Out of the market, the portfolio
  earns cash.

The data was pulled for the original study on 2026-09-29 and 2026-09-30 into a scratch folder
(%TEMP%/canslim2007). `python research/levtrend.py --build <folder>` converts it once into
research/cache/levtrend_<last date>.json, which everything here reads. Reading Shiller's Excel files needs xlrd and
openpyxl, which the project does not install: put the scratch folder's pylib on PYTHONPATH for the build.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent / "cache"
THROUGH = "2026-09-25"
CACHE = CACHE_DIR / f"levtrend_{THROUGH}.json"
START, END = "1950-01-03", THROUGH
SPREAD, FEE_1X, FEE_LEV, SWITCH_COST = 0.5, 0.09, 0.9, 0.001
DECADES = [(f"{y}s", f"{y}-01-01", f"{y + 9}-12-31") for y in range(1950, 2030, 10)]


# ----------------------------------------------------------------------
# The cache


def build_cache(folder: str) -> None:
    """Convert the original study's files into the cache: the provider's prices, dividends and yields as pulled,
    and Shiller's monthly dividend yield (D / 12 / P) and annual rate, exactly as the study read them."""
    src = Path(folder)
    data = json.loads((src / "index_data.json").read_text(encoding="utf-8"))
    nasdaq = json.loads((src / "nasdaq_data.json").read_text(encoding="utf-8"))
    spy_dividends = json.loads((src / "spy_dividends.json").read_text(encoding="utf-8"))
    ie = pd.read_excel(src / "ie_data.xls", sheet_name="Data", header=None, skiprows=8).iloc[:, :3]
    ie.columns = ["date", "P", "D"]
    ie = ie[pd.to_numeric(ie["date"], errors="coerce").notna()]
    monthly = {}
    for _, row in ie.iterrows():
        year, month = int(row["date"]), int(round((row["date"] - int(row["date"])) * 100))
        if not math.isnan(row["D"]):
            monthly[f"{year}-{month:02d}"] = row["D"] / 12 / row["P"]
    c26 = pd.read_excel(src / "chapt26.xlsx", sheet_name="Data", header=None).iloc[7:, [0, 4]]
    c26.columns = ["year", "R"]
    annual = {str(int(y)): float(r) for y, r in zip(c26["year"], c26["R"])
              if str(y).replace(".0", "").isdigit() and not pd.isna(r)}
    closes = lambda series: {d: v["close"] for d, v in series.items()}  # noqa: E731

    def dividends(rows):
        out = {}
        for r in rows:
            if r.get("date"):
                out[r["date"]] = out.get(r["date"], 0.0) + float(r.get("adjDividend") or 0.0)
        return out

    info = json.loads((src / "etf_info.json").read_text(encoding="utf-8"))
    cache = {
        "through": THROUGH,
        "sources": {"prices": "the data provider, pulled 2026-09-29 and 2026-09-30",
                    "dividends_before_1993": "Robert Shiller, ie_data.xls (monthly S&P 500 dividends)",
                    "cash_before_1990": "Robert Shiller, chapt26.xlsx (annual one-year rate)"},
        "gspc": closes(data["gspc"]),
        "shiller_dividend_yield": monthly,
        "shiller_rate": annual,
        "tbill3m": data["tbill3m"],
        "etfs": {sym: closes(series) for sym, series in data["etfs"].items()},
        "etf_dividends": {**{sym: dividends(rows) for sym, rows in data["etf_dividends"].items()},
                          "SPY": dividends(spy_dividends)},
        "nasdaq": {"etfs": {sym: closes(series) for sym, series in nasdaq["etfs"].items()},
                   "dividends": {sym: dividends(rows) for sym, rows in nasdaq["dividends"].items()},
                   "ixic": nasdaq["ixic"]},
        "fees": {**{sym: info[sym]["expenseRatio"] for sym in info},
                 **{sym: nasdaq["info"][sym]["expenseRatio"] for sym in nasdaq["info"]}},
    }
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    print(f"wrote {CACHE}: {len(cache['gspc'])} S&P sessions, {len(monthly)} Shiller months, {len(annual)} rates")


_DATA: dict | None = None


def data() -> dict:
    global _DATA
    if _DATA is None:
        _DATA = json.loads(CACHE.read_text(encoding="utf-8"))
    return _DATA


# ----------------------------------------------------------------------
# The model (as registered)


def index_series():
    """closes {day: close}, sessions, and rows (day, total return from the previous close, cash rate %, calendar
    days since the previous close)."""
    d = data()
    monthly, annual, tbill = d["shiller_dividend_yield"], d["shiller_rate"], d["tbill3m"]
    closes = d["gspc"]
    days = sorted(closes)
    per_month = pd.Series(days).str[:7].value_counts().to_dict()
    tdays = sorted(k for k, v in tbill.items() if v is not None)
    from bisect import bisect_right
    last_yield, rows = None, []
    for prev, day in zip(days, days[1:]):
        month = day[:7]
        if month in monthly:
            last_yield = monthly[month]
        dividend = (last_yield or 0) / per_month[month]
        rate = tbill[tdays[max(0, bisect_right(tdays, day) - 1)]] if day >= "1990-01-02" else annual[day[:4]]
        gap = (date.fromisoformat(day) - date.fromisoformat(prev)).days
        rows.append((day, closes[day] / closes[prev] - 1 + dividend, rate, gap))
    return closes, days, rows


def signals(closes: dict, days: list[str], monthly_check: bool, length: int = 200) -> dict:
    """Position chosen at each close: True = invested. Months end on their last session."""
    sma = pd.Series([closes[d] for d in days]).rolling(length).mean().tolist()
    out, current = {}, None
    for i, day in enumerate(days):
        month_end = i + 1 == len(days) or days[i + 1][:7] != day[:7]
        if not math.isnan(sma[i]) and (not monthly_check or month_end or current is None):
            current = closes[day] > sma[i]
        out[day] = current
    return out


def simulate(rows, signal, leverage, start=START, end=END, *, fee=None, spread=SPREAD, lag=1, cost=SWITCH_COST):
    """Daily equity from 1.0. rows[i] is the return from close i-1 to close i.

    A decision at close i-1-lag fills at close i-lag, so with the registered lag of 1 the decision at close i-2
    fills at close i-1 and earns rows[i]'s return. The switch cost is charged on the new position's first return.
    fee defaults to the registered 0.09% at 1x and 0.9% above.
    """
    fee = (FEE_1X if leverage == 1 else FEE_LEV) if fee is None else fee
    equity, curve, previous, switches = 1.0, [], None, 0
    for i, (day, ret, rate, gap) in enumerate(rows):
        if day < start or day > end:
            continue
        held = True if signal is None else bool(signal[rows[i - 1 - lag][0]])
        if held:
            daily = leverage * ret - (leverage - 1) * (rate + spread) / 100 * gap / 365 - fee / 100 * gap / 365
        else:
            daily = rate / 100 * gap / 365
        if previous is not None and held != previous:
            daily -= cost
            switches += 1
        equity *= 1 + daily
        curve.append((day, equity, held))
        previous = held
    return curve, switches


def stats(curve, switches=0) -> dict:
    days = [c[0] for c in curve]
    values = [c[1] / curve[0][1] for c in curve]      # from the first close, so every year and window is full
    years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    peak, peak_day, dd, when, longest = values[0], days[0], 0.0, None, (0, None)
    for d, v in zip(days, values):
        if v >= peak:
            span = (date.fromisoformat(d) - date.fromisoformat(peak_day)).days
            if span > longest[0]:
                longest = (span, (peak_day, d))
            peak, peak_day = v, d
        elif 1 - v / peak > dd:
            dd, when = 1 - v / peak, (peak_day, d)
    span = (date.fromisoformat(days[-1]) - date.fromisoformat(peak_day)).days
    if span > longest[0]:
        longest = (span, (peak_day, "not recovered"))
    rets = [b / a - 1 for a, b in zip(values, values[1:])]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 252)
    calendar, prev = {}, values[0]
    for i, d in enumerate(days):
        if i + 1 == len(days) or days[i + 1][:4] != d[:4]:
            calendar[d[:4]] = values[i] / prev - 1
            prev = values[i]
    return {"cagr": 100 * (values[-1] / values[0]) ** (1 / years) - 100, "total": 100 * (values[-1] / values[0] - 1),
            "maxdd": 100 * dd, "dd_when": when, "vol": 100 * vol,
            "worst_year": min((100 * v, y) for y, v in calendar.items()),
            "underwater_years": longest[0] / 365.25, "underwater_when": longest[1],
            "switches_per_year": switches / years, "invested": 100 * sum(c[2] for c in curve) / len(curve),
            "calendar": {y: 100 * v for y, v in calendar.items()}}


def cagr_between(curve, start, end) -> float:
    part = [c for c in curve if start <= c[0] <= end]
    before = [c for c in curve if c[0] < start]
    base = before[-1][1] if before else part[0][1]
    years = (date.fromisoformat(part[-1][0]) - date.fromisoformat((before[-1] if before else part[0])[0])).days / 365.25
    return 100 * ((part[-1][1] / base) ** (1 / years) - 1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        build_cache(sys.argv[2])
    else:
        print("usage: python research/levtrend.py --build <scratch folder>")
