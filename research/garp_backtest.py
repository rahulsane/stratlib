"""S&P 500 GARP backtest: (1) a self-managed copy of the index, (2) as if an S&P 500 GARP ETF had existed all along.

Index: garp_index.py (current rules throughout; "historical" rules only for the check against S&P's published
returns). Main period: from the December 2015 rebalancing (effective after the close of 18 Dec 2015) to the
latest session. An extended run from December 2007 is reported separately as approximate, because the data provider lacks
prices for 10-25% of S&P 500 members before 2015 (mostly companies later acquired or bankrupt).

Version 1, self-managed: $100,000 at the first rebalancing close. At each rebalancing close the account trades to
the index weights in whole shares, paying the ground-rules slippage (0.10% per side, 0.25% under $20 as traded;
no commissions). Between rebalancings, dividends and the proceeds of stocks leaving the S&P 500 (sold at their last
close in the index) wait in cash, earning nothing, until the next rebalancing.

Version 2, ETF: the index total return less SPGP's measured all-in cost, 0.35% a year (its NAV trailed the official
index by 0.31-0.38% in each calendar year 2020-2025, against a 0.33-0.36% expense ratio), accrued daily, plus
0.10% slippage on the purchase and on the final sale. A 0.15% version shows the iShares MSCI USA Quality GARP ETF's
expense ratio for comparison (a different index).

Taxes (taxable-account section only): 15% on long-term gains and qualified dividends, 24% on short-term gains,
no state tax. Each year's tax is paid at the next June rebalancing (about when it falls due). Losses offset gains
and carry forward. ETFs: distributions (dividends less fund expenses) taxed yearly; no capital-gain distributions
(SPGP's after-tax figures show none); gains taxed on the final sale.

Run: PYTHONPATH=src .venv/Scripts/python research/garp_backtest.py
"""

from __future__ import annotations

import csv
import json
import math
import sys
import warnings
from bisect import bisect_right
from datetime import date
from pathlib import Path

import numpy as np

import garp_data as GD
import garp_index as GI
from stratlib.app import open_context

warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "garp"
TEMP_TBILL = Path("C:/Users/rahul/AppData/Local/Temp/canslim2007/index_data.json")

START_MAIN, START_EXT = "2015-12", "2007-12"
CAPITAL = 100_000.0
ETF_COST, ETF_SLIP, ISHARES_COST = 0.0035, 0.0010, 0.0015
TAX_LT, TAX_ST = 0.15, 0.24
BENCH = {"SPY": 0.0, "RSP": 0.0, "IVW": 0.0, "QQQ": 0.0, "SPGP": 0.0, "GARP": 0.0}
LAUNCH = "2019-02-25"
OFFICIAL = {2020: 16.31, 2021: 36.07, 2022: -13.54, 2023: 20.68, 2024: 8.92, 2025: 10.21}
SPGP_NAV = {2020: 15.95, 2021: 35.60, 2022: -13.84, 2023: 20.30, 2024: 8.51, 2025: 9.79}


def slip(as_traded: float) -> float:
    return 0.0025 if as_traded < 20 else 0.0010


# ----------------------------------------------------------------------------------------------
# Market data

def tbill_series(ctx) -> dict[str, float]:
    doc = ctx.store.document("research:garp:tbill3m")
    if not doc:
        rows = {}
        if TEMP_TBILL.exists():
            rows.update({d: v for d, v in json.load(open(TEMP_TBILL))["tbill3m"].items() if v is not None})
        rows.update((ctx.store.document("research:tbill3m") or {}).get("rows", {}))
        doc = {"made_on": date.today().isoformat(), "rows": rows}
        ctx.store.save_document("research:garp:tbill3m", doc)
    return doc["rows"]


def etf_total_return(ctx, symbol: str, dates: np.ndarray) -> np.ndarray:
    """Total-return index on `dates` (NaN before the first bar), dividends reinvested at the ex-date close."""
    bars = GD.db_bars(ctx.settings.data.db_path, symbol, "2002-01-01")
    ddoc = ctx.store.document(GD.DIV_KEY + symbol)
    if not ddoc:
        ddoc = GD.fetch_dividends(ctx.client, symbol)
        ctx.store.save_document(GD.DIV_KEY + symbol, ddoc)
    divs = ddoc.get("rows", {})
    out = np.full(len(dates), np.nan)
    prev = None
    for i, d in enumerate(dates):
        bar = bars.get(str(d))
        close = bar[3] if bar else None
        if close is None:
            out[i] = out[i - 1] if i and prev is not None else np.nan
            continue
        if prev is None:
            out[i] = 1.0
        else:
            out[i] = out[i - 1] * (close + divs.get(str(d), 0.0)) / prev
        prev = close
    return out


# ----------------------------------------------------------------------------------------------
# Version 1: self-managed

class Lots:
    """FIFO tax lots per column: [adjusted shares, cost per share, buy row]."""

    def __init__(self):
        self.lots: dict[int, list] = {}

    def buy(self, j, n, cost, row):
        self.lots.setdefault(j, []).append([n, cost, row])

    def sell(self, j, n, proceeds_per_share, row, dates) -> tuple[float, float]:
        st = lt = 0.0
        left = n
        queue = self.lots.get(j, [])
        while left > 1e-9 and queue:
            lot = queue[0]
            take = min(left, lot[0])
            gain = take * (proceeds_per_share - lot[1])
            held = (date.fromisoformat(str(dates[row])) - date.fromisoformat(str(dates[lot[2]]))).days
            if held > 365:
                lt += gain
            else:
                st += gain
            lot[0] -= take
            left -= take
            if lot[0] <= 1e-9:
                queue.pop(0)
        return st, lt

    def unrealized(self, j, price, row, dates) -> tuple[float, float]:
        st = lt = 0.0
        for n, cost, r in self.lots.get(j, []):
            held = (date.fromisoformat(str(dates[row])) - date.fromisoformat(str(dates[r]))).days
            if held > 365:
                lt += n * (price - cost)
            else:
                st += n * (price - cost)
        return st, lt


def tax_due(st: float, lt: float, carry: float) -> tuple[float, float]:
    """Tax on a year's net short- and long-term gains (dividends handled separately) and the new loss carry."""
    st, lt = st, lt
    total = st + lt - carry
    if total <= 0:
        return 0.0, -total
    # Losses in one bucket offset the other; the carry offsets short-term first.
    c = carry
    if st < 0:
        lt, st = lt + st, 0.0
    if lt < 0:
        st, lt = st + lt, 0.0
    use = min(c, st)
    st -= use
    c -= use
    lt -= min(c, lt)
    return TAX_ST * max(st, 0) + TAX_LT * max(lt, 0), 0.0


def self_managed(data: GI.Data, idx: dict, capital: float = CAPITAL, whole: bool = True,
                 taxes: bool = False, end: int | None = None) -> dict:
    dates, px, n, m = data.dates, data.px, *data.px.shape
    end = n - 1 if end is None else end
    s0 = idx["start"]
    shares = np.zeros(m)
    cash = capital
    lots = Lots()
    curve, costs, traded, trades = [], 0.0, 0.0, []
    year_st = year_lt = year_div = 0.0
    carry, owed, paid_total = 0.0, 0.0, 0.0
    tax_log = []
    cur_year = str(dates[s0])[:4]

    def sell(j, nsh, i, why):
        nonlocal cash, costs, traded, year_st, year_lt
        at = px[i, j] * data.factor[i, j]
        fee = nsh * px[i, j] * slip(at)
        cash += nsh * px[i, j] - fee
        costs += fee
        traded += nsh * px[i, j]
        st, lt = lots.sell(j, nsh, px[i, j] * (1 - slip(at)), i, dates)
        year_st += st
        year_lt += lt
        trades.append((str(dates[i]), data.tickers[j], "sell", nsh / data.factor[i, j], at, why))

    def buy(j, nsh, i):
        nonlocal cash, costs, traded
        at = px[i, j] * data.factor[i, j]
        fee = nsh * px[i, j] * slip(at)
        cash -= nsh * px[i, j] + fee
        costs += fee
        traded += nsh * px[i, j]
        lots.buy(j, nsh, px[i, j] * (1 + slip(at)), i)
        trades.append((str(dates[i]), data.tickers[j], "buy", nsh / data.factor[i, j], at, "rebalance"))

    for i in range(s0, end + 1):
        day = str(dates[i])
        if day[:4] != cur_year:                     # year end: work out the tax, pay it in June
            if taxes:
                t, carry = tax_due(year_st, year_lt, carry)
                t += TAX_LT * year_div
                owed += t
                tax_log.append({"year": cur_year, "st": year_st, "lt": year_lt, "div": year_div, "tax": t})
            year_st = year_lt = year_div = 0.0
            cur_year = day[:4]
        if i > s0:
            d = float(shares @ data.div[i])
            cash += d
            year_div += d
        if i in idx["weights"]:
            if taxes and owed and day[5:7] >= "04":           # first rebalance after the April deadline
                cash -= owed
                paid_total += owed
                owed = 0.0
            equity = cash + float(shares @ px[i])
            target = idx["weights"][i]
            want = np.zeros(m)
            for j, w in target.items():
                at = px[i, j] * data.factor[i, j]
                value = w * equity * (1 - 0.002)
                want[j] = (round(value / at) if whole else value / at) * data.factor[i, j]
            for j in np.flatnonzero(shares > want + 1e-9):
                sell(j, shares[j] - want[j], i, "rebalance")
            for j in np.flatnonzero(want > shares + 1e-9):
                buy(j, want[j] - shares[j], i)
            shares = want
            while cash < 0:                          # whole-share rounding overspent: trim the largest holding
                j = int(np.argmax(shares * px[i]))
                one = data.factor[i, j]
                sell(j, one, i, "cash")
                shares[j] -= one
        elif i < n - 1:
            for j in np.flatnonzero(shares > 0):
                if (data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i:
                    sell(j, shares[j], i, "left S&P 500")
                    shares[j] = 0.0
        curve.append(cash + float(shares @ px[i]))
    out = {"curve": np.array(curve), "rows": np.arange(s0, end + 1), "costs": costs, "traded": traded,
           "trades": trades, "final_cash": cash}
    if taxes:
        t, carry2 = tax_due(year_st, year_lt, carry)
        out["tax_paid"] = paid_total
        out["tax_owed_end"] = owed + t + TAX_LT * year_div
        ust = ult = 0.0
        for j in np.flatnonzero(shares > 0):
            a, b = lots.unrealized(j, px[end, j] * (1 - slip(px[end, j] * data.factor[end, j])), end, dates)
            ust += a
            ult += b
        liq, _ = tax_due(ust, ult, carry2)
        out["tax_on_liquidation"] = liq
        out["tax_log"] = tax_log
    return out


# ----------------------------------------------------------------------------------------------
# Version 2: ETF

def etf_curve(tr: np.ndarray, rows: np.ndarray, dates: np.ndarray, cost: float, capital: float = CAPITAL,
              slip_in: float = ETF_SLIP) -> np.ndarray:
    v = np.empty(len(rows))
    v[0] = capital * (1 - slip_in)
    for k in range(1, len(rows)):
        i, h = rows[k], rows[k - 1]
        days = (date.fromisoformat(str(dates[i])) - date.fromisoformat(str(dates[h]))).days
        v[k] = v[k - 1] * tr[i] / tr[h] * (1 - cost) ** (days / 365.25)
    return v


def etf_after_tax(tr, pr_div_yield, rows, dates, cost, capital=CAPITAL) -> dict:
    """ETF held in a taxable account: distributions (dividends less expenses) taxed at 15% each year, paid in June
    by selling shares; the final sale pays 15% on the gain."""
    v = capital * (1 - ETF_SLIP)
    basis = capital
    owed = 0.0
    year, dist = str(dates[rows[0]])[:4], 0.0
    curve = [v]
    paid = 0.0
    for k in range(1, len(rows)):
        i, h = rows[k], rows[k - 1]
        days = (date.fromisoformat(str(dates[i])) - date.fromisoformat(str(dates[h]))).days
        fee = 1 - (1 - cost) ** (days / 365.25)
        dist_today = v * pr_div_yield[i] - v * fee
        v = v * tr[i] / tr[h] * (1 - fee)
        dist += max(dist_today, 0.0)
        basis += max(dist_today, 0.0)            # reinvested distributions add to basis
        d = str(dates[i])
        if d[:4] != year:
            owed += TAX_LT * dist
            dist, year = 0.0, d[:4]
        if owed and d[5:7] == "06" and str(dates[h])[5:7] == "05":
            basis -= basis * owed / v
            v -= owed
            paid += owed
            owed = 0.0
        curve.append(v)
    owed += TAX_LT * dist
    final_sale = v * (1 - ETF_SLIP)
    liq = TAX_LT * max(final_sale - basis, 0.0)
    return {"curve": np.array(curve), "tax_paid": paid, "tax_owed_end": owed, "tax_on_liquidation": liq}


# ----------------------------------------------------------------------------------------------
# Metrics

def monthly(values: np.ndarray, days: list[str]) -> tuple[list[str], np.ndarray]:
    ends = [k for k in range(len(days)) if k == len(days) - 1 or days[k + 1][:7] != days[k][:7]]
    keys = [days[k][:7] for k in ends]
    v = values[ends]
    base = np.r_[values[0], v[:-1]]
    return keys, v / base - 1


def drawdowns(values: np.ndarray, days: list[str], top: int = 5) -> list[dict]:
    peak_i, out, i = 0, [], 0
    peak = values[0]
    k = 0
    episodes = []
    start = None
    trough_i = None
    for k in range(len(values)):
        if values[k] >= peak:
            if start is not None:
                episodes.append((start, trough_i, k))
                start = None
            peak, peak_i = values[k], k
        else:
            if start is None:
                start, trough_i = peak_i, k
            if values[k] < values[trough_i]:
                trough_i = k
    if start is not None:
        episodes.append((start, trough_i, None))
    for a, t, r in episodes:
        out.append({"peak": days[a], "trough": days[t], "recovered": days[r] if r is not None else None,
                    "depth": 100 * (1 - values[t] / values[a]),
                    "days_to_trough": (date.fromisoformat(days[t]) - date.fromisoformat(days[a])).days,
                    "days_underwater": (date.fromisoformat(days[r] if r is not None else days[-1])
                                        - date.fromisoformat(days[a])).days})
    out.sort(key=lambda e: -e["depth"])
    return out[:top]


def metrics(values: np.ndarray, days: list[str], bench: np.ndarray | None, tbill: dict) -> dict:
    v = np.asarray(values, dtype=float)
    years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
    r = v[1:] / v[:-1] - 1
    tdays = sorted(tbill)
    rf = np.array([tbill[tdays[max(0, bisect_right(tdays, d) - 1)]] for d in days[1:]]) / 100 / 252
    ex = r - rf
    downside = np.sqrt(np.mean(np.minimum(ex, 0) ** 2))
    peak = np.maximum.accumulate(v)
    dd = 1 - v / peak
    cagr = (v[-1] / v[0]) ** (1 / years) - 1
    mk, mr = monthly(v, days)
    out = {
        "start": days[0], "end": days[-1], "years": years, "final": float(v[-1]), "total": 100 * (v[-1] / v[0] - 1),
        "cagr": 100 * cagr, "vol": 100 * r.std() * math.sqrt(252),
        "sharpe": ex.mean() / ex.std() * math.sqrt(252) if ex.std() > 0 else float("nan"),
        "sortino": ex.mean() / downside * math.sqrt(252) if downside > 0 else float("nan"),
        "maxdd": 100 * dd.max(), "calmar": cagr / dd.max() if dd.max() > 0 else float("nan"),
        "ulcer": 100 * math.sqrt(np.mean(dd ** 2)),
        "pos_months": 100 * float(np.mean(mr > 0)), "best_month": 100 * float(mr.max()), "worst_month": 100 * float(mr.min()),
        "cvar5_month": 100 * float(np.mean(np.sort(mr)[:max(1, int(0.05 * len(mr)))])),
        "drawdowns": drawdowns(v, days),
    }
    yearly, prev = {}, v[0]
    for k, d in enumerate(days):
        if k + 1 == len(days) or days[k + 1][:4] != d[:4]:
            yearly[d[:4]] = 100 * (v[k] / prev - 1)
            prev = v[k]
    out["yearly"] = yearly
    if bench is not None:
        b = np.asarray(bench, dtype=float)
        rb = b[1:] / b[:-1] - 1
        exb = rb - rf
        beta = np.cov(ex, exb)[0, 1] / exb.var()
        alpha = (ex.mean() - beta * exb.mean()) * 252
        act = r - rb
        te = act.std() * math.sqrt(252)
        bc = (b[-1] / b[0]) ** (1 / years) - 1
        _, bm = monthly(b, days)
        up, dn = bm > 0, bm < 0
        roll = {}
        for w in (36, 60):
            if len(mr) > w:
                gs = np.array([np.prod(1 + mr[k:k + w]) ** (12 / w) - np.prod(1 + bm[k:k + w]) ** (12 / w)
                               for k in range(len(mr) - w + 1)])
                roll[f"{w // 12}y"] = {"windows": len(gs), "beat_pct": 100 * float(np.mean(gs > 0)),
                                       "median": 100 * float(np.median(gs)), "min": 100 * float(gs.min()),
                                       "max": 100 * float(gs.max())}
        out.update({
            "bench_cagr": 100 * bc, "excess_cagr": 100 * (cagr - bc), "beta": float(beta), "alpha": 100 * alpha,
            "corr": float(np.corrcoef(r, rb)[0, 1]), "te": 100 * te,
            "ir": (act.mean() * 252) / te if te > 0 else float("nan"),
            "t_stat": (act.mean() * 252) / te * math.sqrt(years) if te > 0 else float("nan"),
            "up_capture": 100 * float(np.mean(mr[up]) / np.mean(bm[up])) if up.any() else float("nan"),
            "down_capture": 100 * float(np.mean(mr[dn]) / np.mean(bm[dn])) if dn.any() else float("nan"),
            "beat_months": 100 * float(np.mean(mr > bm)), "rolling": roll,
            "years_beat": sum(1 for y in yearly if y in _yearly(b, days) and yearly[y] > _yearly(b, days)[y]),
            "years_total": len(yearly),
        })
    return out


def _yearly(v, days):
    out, prev = {}, v[0]
    for k, d in enumerate(days):
        if k + 1 == len(days) or days[k + 1][:4] != d[:4]:
            out[d[:4]] = 100 * (v[k] / prev - 1)
            prev = v[k]
    return out


# ----------------------------------------------------------------------------------------------
# Holdings analytics

def holdings_stats(data: GI.Data, idx: dict) -> dict:
    plan = idx["plan"]
    names = [set(p["target"]) for p in plan]
    tenure = {}
    for s in names:
        for t in s:
            tenure[t] = tenure.get(t, 0) + 1
    sectors_avg: dict = {}
    for p in plan:
        sec = {}
        for t, w in p["target"].items():
            s = data.sector[data.tickers.index(t)] or "Unknown"
            sec[s] = sec.get(s, 0) + w
        for s, w in sec.items():
            sectors_avg[s] = sectors_avg.get(s, 0) + w / len(plan)
    top10 = [sum(sorted(p["target"].values(), reverse=True)[:10]) for p in plan]
    maxw = [max(p["target"].values()) for p in plan]
    minw = [min(p["target"].values()) for p in plan]
    kept = [len(a & b) for a, b in zip(names, names[1:])]
    return {"rebalances": len(plan), "distinct": len(tenure), "avg_tenure_rebalances": float(np.mean(list(tenure.values()))),
            "kept_per_rebalance": float(np.mean(kept)) if kept else None, "turnover_one_way": idx["turnover"],
            "avg_top10": float(np.mean(top10)), "max_weight": float(max(maxw)), "min_weight": float(min(minw)),
            "sectors_avg": dict(sorted(sectors_avg.items(), key=lambda x: -x[1])),
            "latest": plan[-1]}


def contributions(data: GI.Data, idx: dict, a: int, b: int) -> dict[str, float]:
    """Each stock's contribution to the index's total return between rows a and b (sum of daily weight x return)."""
    contrib = np.zeros(len(data.tickers))
    tr = idx["tr"]
    # Rebuild daily holdings from the plan: weights at each rebalancing drift with price and dividends.
    w = np.zeros(len(data.tickers))
    for i in range(a, b + 1):
        if i in idx["weights"]:
            w[:] = 0
            for j, x in idx["weights"][i].items():
                w[j] = x
            continue
        if i == a:
            continue
        with np.errstate(all="ignore"):
            ret = np.where(data.px[i - 1] > 0, (data.px[i] + data.div[i]) / data.px[i - 1] - 1, 0.0)
        growth = tr[i - 1] / tr[a]
        contrib += growth * w * ret
        w = w * (1 + ret)
        tot = w.sum()
        if tot > 0:
            w /= tot
        gone = [j for j in np.flatnonzero(w > 0) if i < len(data.dates) - 1 and
                ((data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i)]
        if gone:
            w[gone] = 0
            w /= w.sum()
    order = np.argsort(contrib)
    return {"top": [(data.tickers[j], 100 * float(contrib[j])) for j in order[::-1][:10]],
            "bottom": [(data.tickers[j], 100 * float(contrib[j])) for j in order[:10]]}


def ew_universe(data: GI.Data, start: int) -> np.ndarray:
    """Equal-weight total return of the covered S&P 500 members, rebalanced at RSP's quarterly dates (third Friday
    of March, June, September, December), for the survivorship check against RSP."""
    n, m = data.px.shape
    sched = set()
    for y in range(int(str(data.dates[start])[:4]), int(str(data.dates[-1])[:4]) + 1):
        for mo in (3, 6, 9, 12):
            sched.add(GI.session_on_or_before(data.dates, GI.third_friday(y, mo).isoformat()))
    v = np.full(n, np.nan)
    v[start] = 1.0
    shares = np.zeros(m)

    def reset(i, value):
        cols = np.flatnonzero(data.member[i] & (data.px[i] > 0) & (data.member[min(i + 1, n - 1)]))
        s = np.zeros(m)
        s[cols] = value / len(cols) / data.px[i, cols]
        return s

    shares = reset(start, 1.0)
    for i in range(start + 1, n):
        prev = float(shares @ data.px[i - 1])
        v[i] = v[i - 1] * float(shares @ (data.px[i] + data.div[i])) / prev
        if i in sched:
            shares = reset(i, 1.0)
        elif i < n - 1:
            gone = [j for j in np.flatnonzero(shares > 0)
                    if (data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i]
            if gone:
                tot = float(shares @ data.px[i])
                shares[gone] = 0
                shares *= tot / float(shares @ data.px[i])
    return v


# ----------------------------------------------------------------------------------------------

def main() -> None:
    ctx = open_context()
    try:
        data = GI.load_data(ctx)
        tbill = tbill_series(ctx)
        bench = {s: etf_total_return(ctx, s, data.dates) for s in BENCH}
    finally:
        ctx.close()
    dates = data.dates
    days = [str(d) for d in dates]
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {"rules": __doc__, "index_rules": GI.__doc__, "made_on": date.today().isoformat()}

    # --- Index builds
    main_idx = GI.build(data, START_MAIN, "current")
    ext_idx = GI.build(data, START_EXT, "current")
    hist_idx = GI.build(data, "2018-12", "historical")
    s0 = main_idx["start"]
    e0 = ext_idx["start"]
    rows = np.arange(s0, len(dates))
    rows_ext = np.arange(e0, len(dates))

    # Dividend yield of the index per day (for ETF distributions)
    def div_yield(idx):
        y = np.zeros(len(dates))
        y[idx["start"] + 1:] = (idx["tr"][idx["start"] + 1:] / idx["tr"][idx["start"]:-1]) / \
                               (idx["pr"][idx["start"] + 1:] / idx["pr"][idx["start"]:-1]) - 1
        return y

    # --- Validation against the official index and SPGP
    val = {"calendar": {}}
    for y in OFFICIAL:
        a = GI.session_on_or_before(dates, f"{y - 1}-12-31")
        b = GI.session_on_or_before(dates, f"{y}-12-31")
        val["calendar"][y] = {"official": OFFICIAL[y], "spgp_nav": SPGP_NAV[y],
                              "rebuilt_historical_rules": 100 * (hist_idx["tr"][b] / hist_idx["tr"][a] - 1),
                              "rebuilt_current_rules": 100 * (main_idx["tr"][b] / main_idx["tr"][a] - 1),
                              "spgp_market": 100 * (bench["SPGP"][b] / bench["SPGP"][a] - 1)}
    a = GI.session_on_or_before(dates, "2019-06-24")
    live = np.arange(a, len(dates))
    sp = bench["SPGP"][live]
    for name, idx in (("historical", hist_idx), ("current", main_idx)):
        ours = idx["tr"][live]
        re_, ro_ = np.diff(np.log(sp)), np.diff(np.log(ours))
        yrs = (date.fromisoformat(days[live[-1]]) - date.fromisoformat(days[live[0]])).days / 365.25
        val[f"spgp_{name}"] = {"corr": float(np.corrcoef(re_, ro_)[0, 1]), "te": 100 * float((re_ - ro_).std() * math.sqrt(252)),
                               "cagr_spgp": 100 * ((sp[-1] / sp[0]) ** (1 / yrs) - 1),
                               "cagr_rebuilt": 100 * ((ours[-1] / ours[0]) ** (1 / yrs) - 1)}
    results["validation"] = val

    # --- Versions
    v1 = self_managed(data, main_idx)
    v1_frac = self_managed(data, main_idx, whole=False)
    v1_small = self_managed(data, main_idx, capital=10_000.0)
    v1_big = self_managed(data, main_idx, capital=1_000_000.0)
    v2 = etf_curve(main_idx["tr"], rows, dates, ETF_COST)
    v2_ish = etf_curve(main_idx["tr"], rows, dates, ISHARES_COST)
    gross = CAPITAL * main_idx["tr"][rows] / main_idx["tr"][s0]
    spy = CAPITAL * bench["SPY"][rows] / bench["SPY"][s0]
    curves = {"GARP index (no costs)": gross, "Version 1: self-managed": v1["curve"],
              "Version 2: ETF (0.35%/yr)": v2, "ETF at 0.15%/yr": v2_ish,
              "Self-managed, fractional shares": v1_frac["curve"],
              "Self-managed, $10k": v1_small["curve"] * 10, "Self-managed, $1M": v1_big["curve"] / 10}
    for s in ("SPY", "RSP", "IVW", "QQQ"):
        curves[s] = CAPITAL * bench[s][rows] / bench[s][s0]
    # Extras from earlier research: an S&P 500 200-day trend switch and a 50/50 blend with SPY (both on the ETF version)
    ctx = open_context()
    try:
        spy_close = GD.db_bars(ctx.settings.data.db_path, "SPY", "2003-01-01")
    finally:
        ctx.close()
    spy_price = np.array([spy_close[d][3] if d in spy_close else np.nan for d in days])
    for i in range(1, len(days)):
        if not np.isfinite(spy_price[i]):
            spy_price[i] = spy_price[i - 1]
    spy_tr = bench["SPY"]
    sma = np.full(len(dates), np.nan)
    for i in range(199, len(dates)):
        sma[i] = np.mean(spy_price[i - 199:i + 1])
    tdays = sorted(tbill)
    trend, inv = [CAPITAL * (1 - ETF_SLIP)], True
    for k in range(1, len(rows)):
        i, h = rows[k], rows[k - 1]
        gap = (date.fromisoformat(days[i]) - date.fromisoformat(days[h])).days
        if inv:
            val_ = trend[-1] * main_idx["tr"][i] / main_idx["tr"][h] * (1 - ETF_COST) ** (gap / 365.25)
        else:
            rate = tbill[tdays[max(0, bisect_right(tdays, days[h]) - 1)]] / 100
            val_ = trend[-1] * (1 + rate) ** (gap / 365.25)
        month_end = k + 1 < len(rows) and days[rows[k + 1]][:7] != days[i][:7]
        if month_end:
            want = spy_price[i] > sma[i]
            if want != inv:
                val_ *= (1 - ETF_SLIP)
                inv = want
        trend.append(val_)
    curves["ETF + S&P 200-day trend switch"] = np.array(trend)
    blend = [CAPITAL]
    wg = 0.5
    parts = [CAPITAL * 0.5, CAPITAL * 0.5]
    for k in range(1, len(rows)):
        i, h = rows[k], rows[k - 1]
        parts[0] *= v2[k] / v2[k - 1]
        parts[1] *= spy[k] / spy[k - 1]
        if days[i][:4] != days[h][:4]:
            tot = parts[0] + parts[1]
            parts = [tot * 0.5 * (1 - 0.0005), tot * 0.5 * (1 - 0.0005)]
        blend.append(parts[0] + parts[1])
    curves["50/50 ETF + SPY (rebalanced yearly)"] = np.array(blend)

    rdays = [days[i] for i in rows]
    periods = {"Main: Dec 2015 - now": (rdays[0], rdays[-1]), "In-sample 2016-2021": ("2016-01-01", "2021-12-31"),
               "Out-of-sample 2022-now": ("2022-01-01", rdays[-1]),
               "S&P backtest era (to 22 Feb 2019)": (rdays[0], "2019-02-22"),
               "Live index (25 Feb 2019 - now)": (LAUNCH, rdays[-1])}
    table = {}
    for pname, (a_, b_) in periods.items():
        k = [x for x, d in enumerate(rdays) if a_ <= d <= b_]
        if len(k) < 30:
            continue
        sub_days = [rdays[x] for x in k]
        table[pname] = {}
        for cname, cv in curves.items():
            table[pname][cname] = metrics(cv[k], sub_days, spy[k], tbill)
        table[pname]["SPY"] = metrics(spy[k], sub_days, None, tbill)
    results["periods"] = table

    # Actual funds over their own lives
    actual = {}
    for s, since in (("SPGP", "2019-06-24"), ("GARP", "2020-01-17")):
        a = GI.session_on_or_before(dates, since)
        rr = np.arange(a, len(dates))
        sd = [days[i] for i in rr]
        actual[s] = {"fund": metrics(bench[s][rr], sd, bench["SPY"][rr], tbill),
                     "rebuilt_etf": metrics(etf_curve(main_idx["tr"], rr, dates, ETF_COST), sd, bench["SPY"][rr], tbill),
                     "spy": metrics(bench["SPY"][rr], sd, None, tbill)}
    results["actual_funds"] = actual

    # Costs and turnover (version 1)
    yrs = (date.fromisoformat(rdays[-1]) - date.fromisoformat(rdays[0])).days / 365.25
    avg_eq = float(np.mean(v1["curve"]))
    results["costs"] = {"self_managed_costs": v1["costs"], "self_managed_cost_pct_per_year": 100 * v1["costs"] / avg_eq / yrs,
                        "self_managed_traded_per_year_pct": 100 * v1["traded"] / avg_eq / yrs,
                        "trades": len(v1["trades"]), "trades_per_rebalance": len(v1["trades"]) / len(main_idx["plan"]),
                        "final_gap_index_vs_self": float(gross[-1] - v1["curve"][-1]),
                        "final_gap_index_vs_etf": float(gross[-1] - v2[-1])}

    # Taxes
    v1_tax = self_managed(data, main_idx, taxes=True)
    pr = main_idx["pr"]
    dy = div_yield(main_idx)
    etf_tax = etf_after_tax(main_idx["tr"], dy, rows, dates, ETF_COST)
    spy_dy = np.zeros(len(dates))
    # SPY's own dividend yield per day from its total-return and price series
    for i in range(1, len(dates)):
        spy_dy[i] = (spy_tr[i] / spy_tr[i - 1]) / (spy_price[i] / spy_price[i - 1]) - 1
    spy_tax = etf_after_tax(spy_tr, spy_dy, rows, dates, 0.0)       # SPY's fee is already in its price
    tax = {}
    for name, res_, pre in (("Version 1: self-managed", v1_tax, v1_tax["curve"]),
                            ("Version 2: ETF", etf_tax, etf_tax["curve"]), ("SPY", spy_tax, spy_tax["curve"])):
        end_value = pre[-1]
        after_owed = end_value - res_["tax_owed_end"]
        liquidated = after_owed * (1 - (ETF_SLIP if name != "Version 1: self-managed" else 0)) - res_["tax_on_liquidation"]
        tax[name] = {"taxes_paid": res_["tax_paid"],
                     "value_after_taxes_paid": after_owed,
                     "cagr_keep_holding": 100 * ((after_owed / CAPITAL) ** (1 / yrs) - 1),
                     "value_if_sold": liquidated, "cagr_if_sold": 100 * ((liquidated / CAPITAL) ** (1 / yrs) - 1)}
    tax["Version 1: self-managed"]["log"] = v1_tax.get("tax_log")
    results["taxes"] = tax

    # Holdings
    results["holdings"] = holdings_stats(data, main_idx)
    results["contributions"] = contributions(data, main_idx, s0, len(dates) - 1)

    # Extended, approximate run from December 2007 and the survivorship check
    ext_rows = rows_ext
    ext_days = [days[i] for i in ext_rows]
    ext_curves = {"GARP index (no costs)": CAPITAL * ext_idx["tr"][ext_rows] / ext_idx["tr"][e0],
                  "Version 1: self-managed": self_managed(data, ext_idx)["curve"],
                  "Version 2: ETF (0.35%/yr)": etf_curve(ext_idx["tr"], ext_rows, dates, ETF_COST)}
    for s in ("SPY", "RSP", "IVW", "QQQ"):
        ext_curves[s] = CAPITAL * bench[s][ext_rows] / bench[s][e0]
    espy = ext_curves["SPY"]
    ext = {}
    for pname, (a_, b_) in {"Extended: Dec 2007 - now": (ext_days[0], ext_days[-1]),
                            "Dec 2007 - Dec 2015": (ext_days[0], "2015-12-18"),
                            "2008 crisis (to Dec 2009)": (ext_days[0], "2009-12-31")}.items():
        k = [x for x, d in enumerate(ext_days) if a_ <= d <= b_]
        ext[pname] = {c: metrics(cv[k], [ext_days[x] for x in k], espy[k], tbill) for c, cv in ext_curves.items()}
    ew = ew_universe(data, e0)
    rsp = bench["RSP"]
    surv = {}
    for y in range(2008, int(days[-1][:4]) + 1):
        a = GI.session_on_or_before(dates, f"{y - 1}-12-31")
        b = GI.session_on_or_before(dates, f"{y}-12-31")
        surv[y] = {"covered_equal_weight": 100 * (ew[b] / ew[a] - 1), "rsp": 100 * (rsp[b] / rsp[a] - 1)}
    results["extended"] = ext
    results["survivorship"] = surv
    results["coverage"] = {k: v for k, v in data.notes.get("sp500_count", {}).items()}
    results["missing_spans"] = len(data.notes.get("missing_spans", []))
    results["deletions_main"] = [d for d in main_idx["deletions"]]
    results["plan_main"] = [{k: v for k, v in p.items() if k != "scores"} for p in main_idx["plan"]]

    # Files
    json.dump(results, open(OUT / "results.json", "w"), indent=1, default=float)
    with open(OUT / "curves_main.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", *curves])
        for k, d in enumerate(rdays):
            w.writerow([d, *[round(float(c[k]), 2) for c in curves.values()]])
    with open(OUT / "curves_extended.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", *ext_curves])
        for k, d in enumerate(ext_days):
            w.writerow([d, *[round(float(c[k]), 2) for c in ext_curves.values()]])
    with open(OUT / "trades_self_managed.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "side", "shares_as_traded", "price_as_traded", "reason"])
        w.writerows(v1["trades"])
    with open(OUT / "holdings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rebalance", "effective", "ticker", "sector", "target_weight", "growth_z", "qv_z", "eps_growth_3y",
                    "sps_growth_3y", "roe", "leverage", "earnings_yield"])
        for p in main_idx["plan"]:
            for t, wt in sorted(p["target"].items(), key=lambda x: -x[1]):
                s = p["scores"][t]
                w.writerow([p["label"], p["effective"], t, data.sector[data.tickers.index(t)], wt, s["gz"], s["qv"],
                            s["eps_g"], s["sps_g"], s["roe"], s["lev"], s["ep"]])
    np.savez(OUT / "series.npz", dates=np.array(rdays), **{k.replace(" ", "_").replace(":", "").replace("/", "_")
                                                           .replace("(", "").replace(")", "").replace("%", "pct")
                                                           .replace(",", "").replace("$", "").replace("+", "plus")
                                                           .replace("-", "_"): v for k, v in curves.items()})
    summary(results)


def summary(results: dict) -> None:
    for pname, rows in results["periods"].items():
        print(f"\n== {pname}")
        print(f"{'':40}{'CAGR':>7}{'vs SPY':>8}{'maxDD':>7}{'vol':>6}{'Sharpe':>7}{'beta':>6}{'TE':>6}")
        for c, m in rows.items():
            print(f"{c:40}{m['cagr']:6.2f}%{m.get('excess_cagr', 0):+7.2f}%{m['maxdd']:6.1f}%{m['vol']:5.1f}%"
                  f"{m['sharpe']:7.2f}{m.get('beta', 1):6.2f}{m.get('te', 0):5.1f}%")
    print("\nvalidation", json.dumps(results["validation"], indent=1, default=float))
    print("taxes", json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "log"} for k, v in results["taxes"].items()},
                              indent=1, default=float))
    print("costs", results["costs"])


if __name__ == "__main__":
    main()
