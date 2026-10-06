"""Shared data for the Traveling Trader tests.

S&P 500: the series of leverage_trend.py (^GSPC daily closes, Shiller monthly dividends spread over each month's
sessions, 0.09%/yr index-fund fee; cash at Shiller's one-year rate before 1990 and 3-month T-bills after).
QQQ: FMP split-adjusted closes and dividends from 1999-03-10, cash at 3-month T-bills.
VIX: FMP ^VIX daily closes from 1990-01-02 (research/cache/vix.json).
"""

from __future__ import annotations

import json
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

TEMP = "C:/Users/rahul/AppData/Local/Temp/canslim2007/"
sys.path.insert(0, TEMP)
sys.path.insert(0, TEMP + "pylib")
import leverage_trend as lt  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "output" / "traveling_trader"


@dataclass
class Index:
    name: str
    days: np.ndarray      # ISO strings
    price: np.ndarray     # close, price only
    tr: np.ndarray        # total-return index (1.0 at the first session)
    cash: np.ndarray      # cash index (1.0 at the first session)
    rate: np.ndarray      # cash rate, % per year
    dy: np.ndarray        # dividend yield, fraction per year (0 where unknown)
    tbill: dict

    def index_on_or_before(self, day: str) -> int:
        return int(bisect_right(self.days, day)) - 1

    def window(self, start: str, end: str = "9999-12-31") -> np.ndarray:
        return np.flatnonzero((self.days >= start) & (self.days <= end))


def _cash_rate(tbill: dict, tdays: list[str], day: str) -> float:
    return float(tbill[tdays[max(0, bisect_right(tdays, day) - 1)]])


def load_spx() -> Index:
    data, monthly, annual = lt.load()
    closes, days, rows = lt.index_series(data, monthly, annual)
    n = len(days)
    price = np.array([closes[d] for d in days])
    tr, cash, rate, dy = np.ones(n), np.ones(n), np.zeros(n), np.zeros(n)
    last_yield = 0.0
    for k, (day, ret, r, gap) in enumerate(rows, 1):
        tr[k] = tr[k - 1] * (1 + ret - lt.FEE_1X / 100 * gap / 365)
        cash[k] = cash[k - 1] * (1 + r / 100 * gap / 365)
        rate[k] = r
        last_yield = monthly.get(day[:7], last_yield)
        dy[k] = 12 * last_yield
    rate[0], dy[0] = rate[1], dy[1]
    return Index("S&P 500", np.array(days), price, tr, cash, rate, dy, data["tbill3m"])


def load_qqq(spx: Index) -> Index:
    nd = json.load(open(TEMP + "nasdaq_data.json"))
    ser = nd["etfs"]["QQQ"]
    days = sorted(ser)
    price = np.array([ser[d]["close"] for d in days])
    divs = {}
    for r in nd["dividends"]["QQQ"]:
        if r.get("date"):
            divs[r["date"]] = divs.get(r["date"], 0) + float(r.get("adjDividend") or r.get("dividend") or 0)
    tdays = sorted(d for d, v in spx.tbill.items() if v is not None)
    n = len(days)
    tr, cash, rate, dy = np.ones(n), np.ones(n), np.zeros(n), np.zeros(n)
    for k in range(1, n):
        ret = price[k] / price[k - 1] - 1 + divs.get(days[k], 0) / price[k - 1]
        r = _cash_rate(spx.tbill, tdays, days[k])
        gap = (date.fromisoformat(days[k]) - date.fromisoformat(days[k - 1])).days
        tr[k] = tr[k - 1] * (1 + ret - lt.FEE_1X / 100 * gap / 365)
        cash[k] = cash[k - 1] * (1 + r / 100 * gap / 365)
        rate[k] = r
        trailing = sum(v for d, v in divs.items() if days[k - 1] < d <= days[k] or
                       (date.fromisoformat(days[k]) - timedelta(days=365)).isoformat() < d <= days[k])
        dy[k] = trailing / price[k]
    rate[0], dy[0] = rate[1], dy[1]
    return Index("QQQ", np.array(days), price, tr, cash, rate, dy, spx.tbill)


def load_vix() -> dict[str, float]:
    raw = json.load(open(HERE / "cache" / "vix.json"))
    return {d: float(v["close"]) for d, v in raw.items() if v.get("close") is not None}


def vix_on(idx: Index, vix: dict[str, float]) -> np.ndarray:
    """VIX close aligned to the index's sessions (forward-filled), NaN before 1990."""
    vdays = sorted(vix)
    out = np.full(len(idx.days), np.nan)
    for i, d in enumerate(idx.days):
        k = bisect_right(vdays, d) - 1
        if k >= 0 and (date.fromisoformat(d) - date.fromisoformat(vdays[k])).days <= 5:
            out[i] = vix[vdays[k]]
    return out


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def election_day(year: int) -> date:
    d = date(year, 11, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d + timedelta(days=1)


def month_starts(idx: Index, first_ok: int = 0) -> list[int]:
    """Row of the first session of each calendar month (excluding the very first partial month)."""
    return [i for i in range(1, len(idx.days)) if idx.days[i][:7] != idx.days[i - 1][:7] and i >= first_ok]


def drawdown_252(price: np.ndarray) -> np.ndarray:
    import pandas as pd
    hi = pd.Series(price).rolling(252, min_periods=252).max().to_numpy()
    return price / hi - 1


def money_weighted(values: np.ndarray, n: int, contribution: float) -> np.ndarray:
    """Annualized money-weighted return, % per year, for `contribution` at months 0..n-1 and `values` at month n."""
    lo, hi = np.full(len(values), -0.05), np.full(len(values), 0.05)
    for _ in range(80):
        r = (lo + hi) / 2
        safe = np.where(np.abs(r) < 1e-12, 1e-12, r)
        fv = np.where(np.abs(r) < 1e-12, contribution * n,
                      contribution * ((1 + safe) ** n - 1) / safe * (1 + safe))
        lo, hi = np.where(fv < values, r, lo), np.where(fv < values, hi, r)
    return 100 * ((1 + (lo + hi) / 2) ** 12 - 1)


def cagr(v0: float, v1: float, d0: str, d1: str) -> float:
    years = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
    return 100 * ((v1 / v0) ** (1 / years) - 1) if years > 0 and v0 > 0 else float("nan")


def max_drawdown(values: np.ndarray) -> float:
    peak = np.maximum.accumulate(values)
    return 100 * float((values / peak - 1).min())


def yearly_returns(values: np.ndarray, days: np.ndarray) -> dict[str, float]:
    out, last_v, last_y = {}, values[0], days[0][:4]
    for i in range(1, len(days)):
        if days[i][:4] != days[i - 1][:4]:
            out[last_y] = 100 * (values[i - 1] / last_v - 1)
            last_v, last_y = values[i - 1], days[i][:4]
    out[last_y] = 100 * (values[-1] / last_v - 1)
    return out
