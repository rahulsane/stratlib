"""The leveraged S&P 500 trend study of 2026-09-29, rebuilt from its saved data, with the reporting standard of
2026-10-02 added. Report: output/levtrend/report.md (levtrend_report.py). Model and data: levtrend.py.

Parts:
1. registered()  the test as registered: 1x, 2x and 3x, held throughout or switched to cash by a month-end or a
                 daily check of the 200-day average, 1950-2026, against the pass bar set before any result.
2. validation()  the model against SPY, SSO and UPRO; fund_costs() fits each fund's all-in cost to its returns.
3. robustness()  2x with each choice moved one step: average length, fill timing, switch cost, financing spread.
4. modern()      1995 on, on SPY's actual total return and 3-month Treasury yields.
5. rolling()     every real 30-year period (first session of each month, 1950-1996), $100,000 each, at the funds'
                 fitted costs: 2x and 3x with 150-, 200- and 250-day averages, and a 75/25 mix of the two.
6. standard()    the reporting standard: before and after tax, the volatility block, SPY at the same volatility,
                 and 2x through margin instead of a leveraged fund; 1950-2026, 1995 on and 2016 on.
7. dca()         $10,000 in January 1995 and $500 a month after, before tax.
8. nasdaq()      the same rules on QQQ and its leveraged funds from the March 2000 peak, and a price-only check on
                 the Nasdaq Composite from 1971.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_levtrend.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_levtrend.py --report
"""

from __future__ import annotations

import csv
import json
import math
import sys
from bisect import bisect_right
from datetime import date, timedelta

import numpy as np

import levtrend as lt
from engine import OUTPUT
from run_wtt_standard import block
from tax_accounting import END as END_OF_TEST, NO_TAX, Dividend, TaxLedger, TaxRates

OUT = OUTPUT / "levtrend"
CAPITAL = 100_000.0
FUNDS = {"SSO": 2, "UPRO": 3, "SPXL": 3}
FIT_FROM = {"UPRO": "2009-06-25", "SPXL": "2009-06-25"}   # 3x funds: after the first weeks of trading
LENGTHS = (150, 200, 250)
MARGIN_SPREAD, MARGIN_FLOOR = 5.5, 8.0
RATES = TaxRates()
PERIODS = {"full": ("1950-01-03", "1950–2026, index model"), "1995": ("1995-01-03", "1995 on, SPY's actual returns"),
           "2016": ("2016-01-04", "2016 on, SPY's actual returns")}


def decades(curve) -> dict:
    return {name: lt.cagr_between(curve, a, b) for name, a, b in lt.DECADES}


def clean(s: dict) -> dict:
    return {k: v for k, v in s.items() if k != "calendar"}


# ----------------------------------------------------------------------
# 1. The registered test


def registered(closes, days, rows) -> tuple[dict, dict]:
    variants = {}
    for check in ("hold", "monthly", "daily"):
        signal = None if check == "hold" else lt.signals(closes, days, check == "monthly")
        for lev in (1, 2, 3):
            variants[f"{lev}x {check}"] = lt.simulate(rows, signal, lev)
    bench_curve = variants["1x hold"][0]
    bench, bench_dec = lt.stats(bench_curve), decades(bench_curve)
    out = {}
    for name, (curve, switches) in variants.items():
        s, dec = lt.stats(curve, switches), decades(curve)
        wins = sum(dec[n] > bench_dec[n] for n in dec)
        out[name] = {**clean(s), "decades": dec, "decade_wins": wins,
                     "cagr_1995": lt.cagr_between(curve, "1995-01-01", lt.END),
                     "pass": None if name == "1x hold" else
                     bool(s["cagr"] > bench["cagr"] and wins >= 5 and s["maxdd"] <= bench["maxdd"])}
    return out, variants


def crash_1987(closes, days) -> dict:
    """Where the month-end and daily 2x rules stood around the October 1987 crash."""
    out = {}
    for check in ("monthly", "daily"):
        sig = lt.signals(closes, days, check == "monthly")
        exits = [d for a, d in zip(days, days[1:]) if "1987-09-01" <= d <= "1987-11-30" and sig[a] and not sig[d]]
        out[check] = {"signal_out": exits[0] if exits else None, "held_on_crash": bool(sig["1987-10-15"])}
    i = days.index("1987-10-19")
    out["crash_day_index_return"] = 100 * (closes[days[i]] / closes[days[i - 1]] - 1)
    return out


# ----------------------------------------------------------------------
# 2. Validation and the funds' fitted costs


def total_return(prices: dict, dividends: dict) -> dict:
    ds = sorted(prices)
    out = {ds[0]: 1.0}
    for a, b in zip(ds, ds[1:]):
        out[b] = out[a] * (prices[b] + dividends.get(b, 0.0)) / prices[a]
    return out


def series_cagr(series: dict, a: str, b: str) -> float:
    ks = [k for k in series if a <= k <= b]
    years = (date.fromisoformat(ks[-1]) - date.fromisoformat(ks[0])).days / 365.25
    return 100 * ((series[ks[-1]] / series[ks[0]]) ** (1 / years) - 1)


def tbill_on():
    tb = {d: v for d, v in lt.data()["tbill3m"].items() if v is not None}
    tdays = sorted(tb)
    return lambda d: tb[tdays[max(0, bisect_right(tdays, d) - 1)]]


def fund_costs(rows) -> dict:
    """Each fund's all-in cost: the borrowing spread s that makes L x index total return - (L-1)(cash + s) - its
    published fee match the fund's actual total return since launch (calibrate.py of the original study)."""
    d = lt.data()
    fits = {}
    for sym, lev in FUNDS.items():
        tr = total_return(d["etfs"][sym], d["etf_dividends"][sym])
        start = max(next(iter(tr)), FIT_FROM.get(sym, "0"))
        fee = d["fees"][sym]
        actual = series_cagr(tr, start, lt.END)

        def model(spread):
            eq, out = 1.0, {}
            for day, ret, rate, gap in rows:
                if start < day <= lt.END:
                    eq *= 1 + lev * ret - (lev - 1) * (rate + spread) / 100 * gap / 365 - fee / 100 * gap / 365
                    out[day] = eq
            return out

        def cagr_of(m):
            ks = list(m)
            return 100 * ((m[ks[-1]]) ** (365.25 / (date.fromisoformat(ks[-1]) - date.fromisoformat(start)).days) - 1)

        lo, hi = -3.0, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if cagr_of(model(mid)) > actual else (lo, mid)
        spread = (lo + hi) / 2
        m = model(spread)
        gaps = []
        for y in range(int(start[:4]) + 1, int(lt.END[:4]) + 1):
            ys = [k for k in m if k[:4] == str(y) and k in tr]
            prev = [k for k in m if k < ys[0] and k in tr]
            if prev:
                gaps.append(100 * (tr[ys[-1]] / tr[prev[-1]] - m[ys[-1]] / m[prev[-1]]))
        fits[sym] = {"lev": lev, "fee": fee, "spread": spread, "all_in": fee + (lev - 1) * spread, "start": start,
                     "actual_cagr": actual, "fit_error_mean": sum(gaps) / len(gaps),
                     "fit_error_worst": max(gaps, key=abs)}
    return fits


def validation(rows) -> dict:
    d = lt.data()
    rate = tbill_on()
    spy = total_return(d["etfs"]["SPY"], d["etf_dividends"]["SPY"])
    curve, _ = lt.simulate(rows, None, 1, "1993-02-01", lt.END)
    model = {day: v for day, v, _ in curve}
    out = {"spy": [{"from": a, "model": series_cagr(model, a, lt.END), "spy": series_cagr(spy, a, lt.END)}
                   for a in ("1993-02-01", "2006-06-21", "2016-01-04")], "funds": {}}
    for sym, lev in (("SSO", 2), ("UPRO", 3)):
        fund = total_return(d["etfs"][sym], d["etf_dividends"][sym])
        ds = [k for k in sorted(fund) if k in spy]
        sim = {ds[0]: 1.0}
        for a, b in zip(ds, ds[1:]):
            gap = (date.fromisoformat(b) - date.fromisoformat(a)).days
            r = spy[b] / spy[a] - 1
            sim[b] = sim[a] * (1 + lev * r - (lev - 1) * (rate(b) + lt.SPREAD) / 100 * gap / 365
                               - lt.FEE_LEV / 100 * gap / 365)
        diffs = []
        for y in range(int(ds[0][:4]) + 1, int(lt.END[:4]) + 1):
            ys = [k for k in ds if k[:4] == str(y)]
            prev = [k for k in ds if k < ys[0]][-1]
            diffs.append(100 * (fund[ys[-1]] / fund[prev] - sim[ys[-1]] / sim[prev]))
        out["funds"][sym] = {"since": ds[0], "actual": series_cagr(fund, ds[0], lt.END),
                             "model": series_cagr(sim, ds[0], lt.END), "gap_mean": sum(diffs) / len(diffs),
                             "gap_largest": max(diffs, key=abs)}
    return out


# ----------------------------------------------------------------------
# 3. Robustness of 2x


def robustness(closes, days, rows) -> dict:
    """Each choice moved one step. A decade counts as won only when 2x is ahead by 0.25 points or more."""
    bench_curve, _ = lt.simulate(rows, None, 1)
    bench, bench_dec = lt.stats(bench_curve), decades(bench_curve)

    def line(curve, switches):
        s, dec = lt.stats(curve, switches), decades(curve)
        wins = sum(dec[k] > bench_dec[k] + 0.25 for k in dec)
        return {"cagr": s["cagr"], "maxdd": s["maxdd"], "vol": s["vol"], "switches_per_year": s["switches_per_year"],
                "decades_ahead": wins, "pass": bool(s["cagr"] > bench["cagr"] and wins >= 5
                                                    and s["maxdd"] <= bench["maxdd"])}

    out = {}
    for check in ("monthly", "daily"):
        m = check == "monthly"
        base = lt.signals(closes, days, m)
        out[check] = {
            "200-day average, next-close fill (base case)": line(*lt.simulate(rows, base, 2)),
            "150-day average": line(*lt.simulate(rows, lt.signals(closes, days, m, 150), 2)),
            "250-day average": line(*lt.simulate(rows, lt.signals(closes, days, m, 250), 2)),
            "fill at the signal day's close": line(*lt.simulate(rows, base, 2, lag=0)),
            "fill two closes later": line(*lt.simulate(rows, base, 2, lag=2)),
            "switch cost 0.25%": line(*lt.simulate(rows, base, 2, cost=0.0025)),
            "financing spread 1.0%": line(*lt.simulate(rows, base, 2, spread=1.0)),
        }
    return out


# ----------------------------------------------------------------------
# 4. On SPY's actual returns from 1995


def spy_rows(start: str) -> list[tuple]:
    """(day, total return, 3-month T-bill %, calendar days) from SPY's closes and dividends."""
    d = lt.data()
    prices, divs, rate = d["etfs"]["SPY"], d["etf_dividends"]["SPY"], tbill_on()
    sd = [k for k in sorted(prices) if k >= start]
    return [(b, (prices[b] + divs.get(b, 0.0)) / prices[a] - 1, rate(b),
             (date.fromisoformat(b) - date.fromisoformat(a)).days) for a, b in zip(sd, sd[1:])]


def modern(closes, days) -> dict:
    mrows = spy_rows("1994-12-01")
    out = {}
    curve, _ = lt.simulate(mrows, None, 1, "1995-01-03", fee=0.0)   # SPY's price already carries its fee
    s = lt.stats(curve)
    out["SPY buy and hold"] = {"cagr": s["cagr"], "maxdd": s["maxdd"], "vol": s["vol"], "worst_year": s["worst_year"]}
    for check in ("monthly", "daily"):
        sig = lt.signals(closes, days, check == "monthly")
        for lev in (1, 2):
            c, n = lt.simulate(mrows, sig, lev, "1995-01-03")
            s = lt.stats(c, n)
            out[f"{lev}x {check}"] = {"cagr": s["cagr"], "maxdd": s["maxdd"], "vol": s["vol"],
                                      "switches_per_year": s["switches_per_year"], "worst_year": s["worst_year"]}
    return out


# ----------------------------------------------------------------------
# 5. Every real 30-year period


def fund_curve(rows, signal, fit: dict):
    """As lt.simulate, with a fund's own fee and fitted spread; also the worst single day."""
    curve, switches = lt.simulate(rows, signal, fit["lev"], fee=fit["fee"], spread=fit["spread"])
    worst_day = min(b[1] / a[1] - 1 for a, b in zip(curve, curve[1:]))
    return curve, switches, worst_day


def mix_path(g2: np.ndarray, g3: np.ndarray, i0: int, i30: int, rebalance: bool, year_starts: np.ndarray,
             start: float = CAPITAL) -> np.ndarray:
    """Daily value of a 75% 2x / 25% 3x mix from i0 to i30 inclusive, restored to 75/25 at the last close of each
    year (rebalance) or left to drift."""
    if not rebalance:
        return start * (0.75 * g2[i0:i30 + 1] / g2[i0] + 0.25 * g3[i0:i30 + 1] / g3[i0])
    cuts = [i0] + [int(i) for i in year_starts if i0 < i <= i30] + [i30 + 1]
    parts, value = [], start
    for a, b in zip(cuts, cuts[1:]):
        anchor = a if a == i0 else a - 1
        seg = value * (0.75 * g2[a:b] / g2[anchor] + 0.25 * g3[a:b] / g3[anchor])
        parts.append(seg)
        value = seg[-1]
    return np.concatenate(parts)


def path_stats(cdays: list[str], values: np.ndarray, switches: int = 0) -> dict:
    s = lt.stats([(d, float(v), True) for d, v in zip(cdays, values)], switches)
    r = values[1:] / values[:-1] - 1
    return {**clean(s), "worst_day": 100 * float(r.min())}


def windows_30y(cdays: list[str]) -> list[tuple]:
    pos = {d: i for i, d in enumerate(cdays)}
    firsts = [d for i, d in enumerate(cdays) if i and d[:7] != cdays[i - 1][:7]]

    def index_after(start, yrs):
        target = (date.fromisoformat(start) + timedelta(days=round(365.25 * yrs))).isoformat()
        return pos[[d for d in cdays[pos[start]:pos[start] + 260 * yrs + 400] if d <= target][-1]]

    out = []
    for d in firsts:
        if (date.fromisoformat(d) + timedelta(days=round(365.25 * 30))).isoformat() > lt.END:
            break
        out.append((d, pos[d], index_after(d, 1), index_after(d, 3), index_after(d, 5), index_after(d, 10),
                    index_after(d, 30)))
    return out


def rolling(closes, days, rows, fits) -> tuple[dict, dict, list]:
    spy_curve, _ = lt.simulate(rows, None, 1, fee=lt.FEE_1X, spread=0.0)
    cdays = [d for d, _, _ in spy_curve]
    windows = windows_30y(cdays)
    year_starts = np.array([i for i, d in enumerate(cdays) if i and d[:4] != cdays[i - 1][:4]])
    paths = {"SPY buy and hold": (np.array([v for _, v, _ in spy_curve]), path_stats(cdays, np.array(
        [v for _, v, _ in spy_curve])))}
    sigs = {(check, n): lt.signals(closes, days, check == "monthly", n) for check in ("monthly", "daily") for n in LENGTHS}
    eqs = {}
    for fund, lev in (("SSO", 2), ("UPRO", 3)):
        for (check, n), sig in sigs.items():
            curve, switches, worst_day = fund_curve(rows, sig, fits[fund])
            eq = np.array([v for _, v, _ in curve])
            name = f"{lev}x {check} {n}-day"
            eqs[name] = eq
            paths[name] = (eq, {**clean(lt.stats(curve, switches)), "worst_day": 100 * worst_day})

    def window_ends(path_of) -> dict:
        end, below3, below5, dd5, dd30 = [], [], [], [], []
        for d, i0, i1, i3, i5, i10, i30 in windows:
            p = path_of(i0, i30)
            end.append(p[-1])
            below3.append(p[i3 - i0] < CAPITAL)
            below5.append(p[i5 - i0] < CAPITAL)
            dd = 1 - p / np.maximum.accumulate(p)
            dd5.append(dd[:i5 - i0 + 1].max())
            dd30.append(dd.max())
        return {"end": np.array(end), "below3": float(np.mean(below3)), "below5": float(np.mean(below5)),
                "dd5": np.array(dd5), "dd30": np.array(dd30)}

    ends = {name: window_ends(lambda i0, i30, eq=eq: CAPITAL * eq[i0:i30 + 1] / eq[i0])
            for name, (eq, _) in paths.items()}
    full = {name: s for name, (_, s) in paths.items()}
    for n in (200, 250):
        g2, g3 = eqs[f"2x monthly {n}-day"], eqs[f"3x monthly {n}-day"]
        for label, reb in (("rebalanced", True), ("never rebalanced", False)):
            name = f"mix {label} monthly {n}-day"
            ends[name] = window_ends(lambda i0, i30, reb=reb: mix_path(g2, g3, i0, i30, reb, year_starts))
            whole = mix_path(g2, g3, 0, len(g2) - 1, reb, year_starts, 1.0)
            full[name] = {**path_stats(cdays, whole), "switches_per_year": full[f"2x monthly {n}-day"]["switches_per_year"]}
    six = [f"2x {c} {n}-day" for c in ("monthly", "daily") for n in LENGTHS]
    ends["six 2x rules, split equally"] = {"end": np.mean([ends[k]["end"] for k in six], axis=0)}

    spy = ends["SPY buy and hold"]["end"]
    out = {}
    for name, e in ends.items():
        v = e["end"]
        twin = None
        if name.startswith("3x"):
            twin = ends[name.replace("3x", "2x")]["end"]
        elif name.startswith("mix"):
            twin = ends[f"2x monthly {name.split()[-1]}"]["end"]
        base200 = None
        if name[:2] in ("2x", "3x") and "200-day" not in name:
            base200 = ends[name.rsplit(" ", 1)[0] + " 200-day"]["end"]
        row = {"worst": float(v.min()), "worst_start": windows[int(np.argmin(v))][0],
               "p10": float(np.percentile(v, 10)), "median": float(np.median(v)), "p90": float(np.percentile(v, 90)),
               "best": float(v.max()), "beat_spy": None if name.startswith("SPY") else 100 * float((v > spy).mean()),
               "beat_2x": None if twin is None else 100 * float((v > twin).mean()),
               "beat_200": None if base200 is None else 100 * float((v > base200).mean())}
        if "dd5" in e:
            row.update(below3=100 * e["below3"], below5=100 * e["below5"],
                       dd5_median=100 * float(np.median(e["dd5"])), dd5_p90=100 * float(np.percentile(e["dd5"], 90)),
                       dd5_worst=100 * float(e["dd5"].max()), dd30_median=100 * float(np.median(e["dd30"])),
                       dd30_worst=100 * float(e["dd30"].max()))
        if name in full:
            f = full[name]
            row.update(cagr=f["cagr"], maxdd=f["maxdd"], worst_day=f["worst_day"], worst_year=f["worst_year"],
                       switches_per_year=f["switches_per_year"])
        out[name] = row

    # Notable starts, to 30 years or to the end of the data
    starts = ["1966-01-03", "1973-10-01", "1987-10-01", "2000-01-03", "2007-10-01", "2022-01-03"]
    pos = {d: i for i, d in enumerate(cdays)}
    g2, g3 = eqs["2x monthly 200-day"], eqs["3x monthly 200-day"]
    curves = {"SPY buy and hold": paths["SPY buy and hold"][0], "2x monthly 200-day": eqs["2x monthly 200-day"],
              "2x daily 200-day": eqs["2x daily 200-day"], "3x monthly 200-day": g3}
    notable = {}
    for s in starts:
        i0 = pos[s]
        marks = {}
        for yrs in (1, 3, 10, 30):
            target = (date.fromisoformat(s) + timedelta(days=round(365.25 * yrs))).isoformat()
            marks[f"{yrs}y"] = None if target > lt.END else bisect_right(cdays, target) - 1
        last = len(cdays) - 1
        row = {}
        for name, eq in curves.items():
            row[name] = {k: (None if i is None else float(CAPITAL * eq[i] / eq[i0])) for k, i in marks.items()}
            row[name]["end"] = float(CAPITAL * eq[last] / eq[i0])
        mix = mix_path(g2, g3, i0, last, True, year_starts)
        row["mix rebalanced monthly 200-day"] = {k: (None if i is None else float(mix[i - i0])) for k, i in marks.items()}
        row["mix rebalanced monthly 200-day"]["end"] = float(mix[-1])
        notable[s] = {"years_to_end": (date.fromisoformat(lt.END) - date.fromisoformat(s)).days / 365.25, "values": row}

    table = [{"start": w[0], **{name: float(e["end"][k]) for name, e in ends.items()}} for k, w in enumerate(windows)]
    return out, notable, table


# ----------------------------------------------------------------------
# 6. The reporting standard


def margin_rate(cash_rate: float) -> float:
    return max(cash_rate + MARGIN_SPREAD, MARGIN_FLOOR)


def standard_rows(start: str) -> list[dict]:
    """Sessions from the one before `start`: price return, dividend yield, cash rate and calendar days. Before 1995
    the index model (S&P closes, Shiller dividends spread over the month's sessions); from 1995 SPY's own closes and
    dividends."""
    if start < "1995-01-01":
        d = lt.data()
        closes, days, rows = lt.index_series()
        monthly = d["shiller_dividend_yield"]
        per_month: dict[str, int] = {}
        for day in days:
            per_month[day[:7]] = per_month.get(day[:7], 0) + 1
        out, last_yield = [], None
        for (day, ret, rate, gap), prev in zip(rows, days):
            if day[:7] in monthly:
                last_yield = monthly[day[:7]]
            dy = (last_yield or 0) / per_month[day[:7]]
            out.append({"day": day, "prev": prev, "pr": closes[day] / closes[prev] - 1, "dy": dy, "rate": rate,
                        "gap": gap, "spy_fee": lt.FEE_1X})
    else:
        d = lt.data()
        prices, divs, rate = d["etfs"]["SPY"], d["etf_dividends"]["SPY"], tbill_on()
        sd = sorted(prices)
        out = [{"day": b, "prev": a, "pr": prices[b] / prices[a] - 1, "dy": divs.get(b, 0.0) / prices[a],
                "rate": rate(b), "gap": (date.fromisoformat(b) - date.fromisoformat(a)).days, "spy_fee": 0.0}
               for a, b in zip(sd, sd[1:])]
    i0 = next(i for i, r in enumerate(out) if r["day"] >= start)
    return out[i0:]


class Account:
    """A taxable account holding funds (keys), SPY units, or cash; dividends, interest and taxes go through cash.

    Tax: each fund or SPY purchase is a lot in a TaxLedger; T-bill interest is ordinary income; SPY dividends are
    qualified when the position is older than 60 days; margin interest is deductible. The year's tax is paid at the
    first session of the next year, from cash, selling holdings pro rata when cash is short (except with margin,
    where the loan grows until the next rebalance)."""

    def __init__(self, rates: TaxRates, cash_earns: bool, margin: bool):
        self.ledger = TaxLedger(rates)
        self.units: dict[str, float] = {}
        self.price: dict[str, float] = {}
        self.cash = CAPITAL
        self.cash_earns, self.margin = cash_earns, margin
        self.interest_income = self.interest_paid = self.taxes = 0.0
        self.payments: list[float] = []
        self.month_ordinary = 0.0
        self.month_dividends: dict[str, float] = {}
        self.entered: dict[str, date] = {}

    def value(self) -> float:
        return self.cash + sum(u * self.price[k] for k, u in self.units.items())

    def buy(self, key: str, day: date, amount: float, cost: float = 0.0) -> None:
        if amount <= 0:
            return
        units = amount * (1 - cost) / self.price[key]
        self.ledger.buy(key, day, units, amount)
        if self.units.get(key, 0.0) <= 1e-12:
            self.entered[key] = day
        self.units[key] = self.units.get(key, 0.0) + units
        self.cash -= amount

    def sell(self, key: str, day: date, units: float, cost: float = 0.0, reason: str = "") -> None:
        units = min(units, self.units.get(key, 0.0))
        if units <= 1e-12:
            return
        proceeds = units * self.price[key] * (1 - cost)
        self.ledger.sell(key, day, units, proceeds, reason)
        self.units[key] -= units
        self.cash += proceeds

    def flush_month(self, day: date) -> None:
        """Monthly tax records: interest earned as ordinary income, dividends qualified unless the position is new."""
        if self.month_ordinary:
            self.ledger.dividends.append(Dividend("cash", day, self.month_ordinary, day))
            self.month_ordinary = 0.0
        for key, amount in self.month_dividends.items():
            if amount:
                self.ledger.dividends.append(Dividend(key, day, amount, self.entered.get(key, day)))
                self.ledger.stats["dividends"] += amount
        self.month_dividends = {}

    def accrue_cash(self, day: date, rate: float, gap: int) -> None:
        if self.cash > 0 and self.cash_earns:
            earned = self.cash * rate / 100 * gap / 365
            self.cash += earned
            self.interest_income += earned
            self.month_ordinary += earned
        elif self.cash < 0:
            charge = -self.cash * margin_rate(rate) / 100 * gap / 365
            self.cash -= charge
            self.ledger.interest(day, charge)
            self.interest_paid += charge

    def pay_tax(self, day: date) -> None:
        due = self.ledger.settle(day.year - 1, day)
        if due <= 0:
            return
        self.payments.append(100 * due / self.value())
        self.taxes += due
        short = due - max(self.cash, 0.0)
        if short > 0 and not self.margin:
            held = {k: u * self.price[k] for k, u in self.units.items() if u > 1e-12}
            total = sum(held.values())
            for k, v in held.items():
                self.sell(k, day, (short * v / total) / self.price[k])
        self.cash -= due

    def finish(self, day: date) -> dict:
        self.flush_month(day)
        held = self.ledger.finish(day.year, day)["tax_held"]
        marked = self.value()
        for k in list(self.units):
            self.sell(k, day, self.units[k], 0.0, END_OF_TEST)
        result = self.ledger.finish(day.year, day)
        carry = result["carry_held"]
        return {"marked": marked, "held": marked - held, "sold": marked - result["tax_sold"],
                "taxes_total": self.taxes + held, "tax_per_year_pct": float(np.mean(self.payments)) if self.payments else 0.0,
                "interest_income": self.interest_income, "interest_paid": self.interest_paid,
                "carry_st": carry[0], "carry_lt": carry[1], "stats": dict(self.ledger.stats)}


def run_account(rows: list[dict], signal: dict | None, kind: str, rates: TaxRates, *, funds: dict | None = None,
                fits: dict | None = None, leverage: float = 1.0) -> dict:
    """kind:
    "funds"   hold the leveraged funds in `funds` ({key: weight}) while the signal is in, cash (T-bills) when out;
              a 75/25 mix is restored at the last close of each year. 0.1% of the value moved on each switch.
    "margin"  hold `leverage` x equity of SPY while the signal is in (all the time without a signal), rebalanced
              at each month's first close and on entry, borrowing at the margin rate; out of the market, cash in
              T-bills (with a signal) or idle (without). 0.1% on each switch, none on rebalancing.
    "spy"     SPY bought at the first close, dividends reinvested at the close, the year's tax paid by selling SPY.
    """
    acct = Account(rates, cash_earns=kind != "spy" and signal is not None, margin=kind == "margin")
    first = date.fromisoformat(rows[0]["day"])
    keys = list(funds) if kind == "funds" else ["SPY"]
    for k in keys:
        acct.price[k] = 1.0
    held = True if signal is None else bool(signal[rows[0]["prev"]])
    if held:
        if kind == "funds":
            for k, w in funds.items():
                acct.buy(k, first, w * CAPITAL)
        else:
            acct.buy("SPY", first, leverage * CAPITAL if kind == "margin" else CAPITAL)
    dates, equity, switches = [rows[0]["day"]], [CAPITAL], 0
    for i, r in enumerate(rows[1:], 1):
        day = date.fromisoformat(r["day"])
        prev_day = date.fromisoformat(rows[i - 1]["day"])
        if day.month != prev_day.month:
            acct.flush_month(prev_day)
        # the day's market move, dividends and interest
        spy_units = acct.units.get("SPY", 0.0)
        div_cash = spy_units * acct.price["SPY"] * r["dy"] if "SPY" in acct.price else 0.0
        for k in keys:
            if k == "SPY":
                acct.price[k] *= 1 + r["pr"] - r["spy_fee"] / 100 * r["gap"] / 365
            else:
                f = fits[k]
                acct.price[k] *= (1 + f["lev"] * (r["pr"] + r["dy"]) - (f["lev"] - 1) * (r["rate"] + f["spread"]) / 100
                                  * r["gap"] / 365 - f["fee"] / 100 * r["gap"] / 365)
        acct.accrue_cash(day, r["rate"], r["gap"])
        if div_cash:
            acct.cash += div_cash
            acct.month_dividends["SPY"] = acct.month_dividends.get("SPY", 0.0) + div_cash
            if kind == "spy":
                acct.buy("SPY", day, div_cash)
        if day.year != prev_day.year:
            acct.pay_tax(day)
        # decisions at this close
        target = True if signal is None else bool(signal[r["prev"]])
        if target != held:
            switches += 1
            if target:
                amount = acct.value()
                if kind == "funds":
                    for k, w in funds.items():
                        acct.buy(k, day, w * amount, lt.SWITCH_COST)
                else:
                    acct.cash -= lt.SWITCH_COST * amount
                    acct.buy("SPY", day, leverage * acct.value())
            else:
                for k in keys:
                    if kind == "margin":
                        acct.cash -= lt.SWITCH_COST * acct.units.get(k, 0.0) * acct.price[k] / leverage
                    acct.sell(k, day, acct.units.get(k, 0.0), lt.SWITCH_COST if kind == "funds" else 0.0)
            held = target
        elif held and kind == "margin" and day.month != prev_day.month:
            want = leverage * acct.value() / acct.price["SPY"]
            have = acct.units.get("SPY", 0.0)
            if want > have:
                acct.buy("SPY", day, (want - have) * acct.price["SPY"])
            else:
                acct.sell("SPY", day, have - want)
        elif held and kind == "spy" and acct.cash < -1e-9:
            acct.sell("SPY", day, -acct.cash / acct.price["SPY"])
        elif held and kind == "funds" and len(funds) > 1 and i + 1 < len(rows) and rows[i + 1]["day"][:4] != r["day"][:4]:
            total = sum(acct.units[k] * acct.price[k] for k in funds)
            for k, w in funds.items():
                diff = w * total - acct.units[k] * acct.price[k]
                if diff < 0:
                    acct.sell(k, day, -diff / acct.price[k])
            for k, w in funds.items():
                diff = w * total - acct.units[k] * acct.price[k]
                if diff > 0:
                    acct.buy(k, day, min(diff, acct.cash))
        dates.append(r["day"])
        equity.append(acct.value())
    end = acct.finish(date.fromisoformat(rows[-1]["day"]))
    years = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365.25
    cagr = lambda v: 100 * ((v / CAPITAL) ** (1 / years) - 1)  # noqa: E731
    return {"dates": dates, "equity": np.array(equity), "years": years, "switches_per_year": switches / years,
            "cagr": cagr(end["marked"]), "cagr_held": cagr(end["held"]), "cagr_sold": cagr(end["sold"]), **end}


def blend_fit(lev: float, fits: dict, spy_fee: float) -> dict:
    """A constant `lev` x exposure built from the same funds, reset daily: SPY and SSO between 1x and 2x, SSO and
    UPRO between 2x and 3x, at their fees and fitted financing."""
    sso, upro = fits["SSO"], fits["UPRO"]
    if lev <= 2:
        w = lev - 1                         # share in SSO, the rest in SPY
        fee = w * sso["fee"] + (1 - w) * spy_fee
        financing = w * sso["spread"]       # SSO borrows 1x its value
    else:
        w = lev - 2                         # share in UPRO, the rest in SSO
        fee = w * upro["fee"] + (1 - w) * sso["fee"]
        financing = 2 * w * upro["spread"] + (1 - w) * sso["spread"]
    return {"lev": lev, "fee": fee, "spread": financing / (lev - 1) if lev > 1 else 0.0}


MAIN = "2x, month-end check"
STRATEGIES = {
    "2x, month-end check": ("funds", "monthly", {"SSO": 1.0}),
    "2x, daily check": ("funds", "daily", {"SSO": 1.0}),
    "3x, month-end check": ("funds", "monthly", {"UPRO": 1.0}),
    "75/25 mix, month-end check": ("funds", "monthly", {"SSO": 0.75, "UPRO": 0.25}),
    "2x through margin, month-end check": ("margin", "monthly", None),
}


def standard(closes, days, fits) -> tuple[dict, dict]:
    sigs = {"monthly": lt.signals(closes, days, True), "daily": lt.signals(closes, days, False)}
    out, curves = {}, {}
    for period, (start, label) in PERIODS.items():
        rows = standard_rows(start)
        res = {}
        for taxed in (False, True):
            rates = RATES if taxed else NO_TAX
            spy = run_account(rows, None, "spy", rates)
            res.setdefault("SPY", {})["taxed" if taxed else "pre"] = spy
            for name, (kind, check, funds) in STRATEGIES.items():
                run = run_account(rows, sigs[check], kind, rates, funds=funds, fits=fits, leverage=2.0)
                res.setdefault(name, {})["taxed" if taxed else "pre"] = run
        bench = res["SPY"]["pre"]["equity"]
        spy_vol = block(res["SPY"]["pre"]["dates"], bench, bench)["vol"]
        summary = {}
        for name, r in res.items():
            pre, tax = r["pre"], r["taxed"]
            blk = block(pre["dates"], pre["equity"], bench)
            row = {"cagr": pre["cagr"], "cagr_held": tax["cagr_held"], "cagr_sold": tax["cagr_sold"],
                   "end_pre": pre["marked"], "end_held": tax["held"], "end_sold": tax["sold"],
                   "switches_per_year": pre["switches_per_year"], "block": {k: v for k, v in blk.items() if k != "yearly"},
                   "yearly": blk["yearly"], "tax": tax_summary(tax), "interest_paid_pre": pre["interest_paid"]}
            if name != "SPY":
                lev = blk["vol"] / spy_vol
                m_pre = run_account(rows, None, "margin", NO_TAX, leverage=lev)
                m_tax = run_account(rows, None, "margin", RATES, leverage=lev)
                row["same_vol"] = {"leverage": lev, "cagr": m_pre["cagr"], "cagr_held": m_tax["cagr_held"],
                                   "cagr_sold": m_tax["cagr_sold"],
                                   "max_drawdown": block(m_pre["dates"], m_pre["equity"], bench)["max_drawdown"]}
                blend = {"BLEND": blend_fit(lev, fits, rows[0]["spy_fee"])}
                f_pre = run_account(rows, None, "funds", NO_TAX, funds={"BLEND": 1.0}, fits=blend)
                f_tax = run_account(rows, None, "funds", RATES, funds={"BLEND": 1.0}, fits=blend)
                row["same_vol_funds"] = {"leverage": lev, "cagr": f_pre["cagr"], "cagr_held": f_tax["cagr_held"],
                                         "cagr_sold": f_tax["cagr_sold"],
                                         "max_drawdown": block(f_pre["dates"], f_pre["equity"], bench)["max_drawdown"],
                                         "fit": blend["BLEND"]}
            summary[name] = row
        first_rate = rows[0]["rate"]
        out[period] = {"label": label, "start": rows[0]["day"], "end": rows[-1]["day"], "strategies": summary,
                       "margin_rate_start": margin_rate(first_rate)}
        if period == "full":
            curves = {name: r["pre"]["equity"] for name, r in res.items()} | {"dates": res["SPY"]["pre"]["dates"]}
            blend = {"BLEND": blend_fit(summary[MAIN]["same_vol"]["leverage"], fits, rows[0]["spy_fee"])}
            curves["SPY at the same volatility as 2x month-end, through the funds"] = run_account(
                rows, None, "funds", NO_TAX, funds={"BLEND": 1.0}, fits=blend)["equity"]
    return out, curves


def tax_summary(run: dict) -> dict:
    st = run["stats"]
    gains = st.get("realized_st_gains", 0.0) + st.get("realized_lt_gains", 0.0)
    return {"taxes_total": run["taxes_total"], "tax_per_year_pct": run["tax_per_year_pct"],
            "st_share_of_gains": 100 * st.get("realized_st_gains", 0.0) / gains if gains else None,
            "realized_gains": gains, "realized_losses": st.get("realized_st_losses", 0.0) + st.get("realized_lt_losses", 0.0),
            "wash_sales": int(st.get("wash_sales", 0)), "wash_disallowed": st.get("wash_disallowed", 0.0),
            "carry_st": run["carry_st"], "carry_lt": run["carry_lt"], "dividends": st.get("dividends", 0.0),
            "interest_income": run["interest_income"], "interest_paid": run["interest_paid"]}


# ----------------------------------------------------------------------
# 7. A monthly saver from 1995


def dca(closes, days, fits) -> dict:
    """$10,000 at the close on 1995-01-03, $500 at the close of the first session of each later month."""
    rows = spy_rows("1994-12-01")
    start = "1995-01-03"

    def run(signal, lev, fee, spread):
        value, flows, curve, previous = 0.0, [], [], None
        for i, (day, ret, rate, gap) in enumerate(rows):
            if day < start:
                continue
            if day == start:
                value, previous = 10_000.0, (True if signal is None else bool(signal[rows[i - 1][0]]))
                flows.append((day, -10_000.0))
                curve.append((day, value))
                continue
            held = True if signal is None else bool(signal[rows[i - 2][0]])
            daily = (lev * ret - (lev - 1) * (rate + spread) / 100 * gap / 365 - fee / 100 * gap / 365) if held \
                else rate / 100 * gap / 365
            if held != previous:
                daily -= lt.SWITCH_COST
            value *= 1 + daily
            previous = held
            if day[:7] != rows[i - 1][0][:7]:
                value += 500.0
                flows.append((day, -500.0))
            curve.append((day, value))
        flows.append((rows[-1][0], value))
        return curve, flows

    def xirr(flows):
        t0 = date.fromisoformat(flows[0][0])
        npv = lambda r: sum(f / (1 + r) ** ((date.fromisoformat(d) - t0).days / 365.25) for d, f in flows)  # noqa: E731
        lo, hi = -0.9, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
        return 100 * mid

    def worst_fall(curve):
        peak, peak_day, worst = 0.0, None, (0.0, None, None)
        for d, v in curve:
            if v > peak:
                peak, peak_day = v, d
            if 1 - v / peak > worst[0]:
                worst = (1 - v / peak, peak_day, d)
        return {"pct": 100 * worst[0], "from": worst[1], "to": worst[2]}

    sso = fits["SSO"]
    runs = {"SPY buy and hold": run(None, 1, 0.0, 0.0),
            "2x, month-end check": run(lt.signals(closes, days, True), 2, sso["fee"], sso["spread"]),
            "2x, daily check": run(lt.signals(closes, days, False), 2, sso["fee"], sso["spread"])}
    paid = -sum(f for _, f in runs["SPY buy and hold"][1][:-1])
    out = {"paid": paid, "deposits": len(runs["SPY buy and hold"][1]) - 1, "runs": {}}
    marks = ["1999-12-31", "2002-10-09", "2007-10-09", "2009-03-09", "2019-12-31", "2020-03-23", "2021-12-31",
             "2022-10-12", lt.END]
    for name, (curve, flows) in runs.items():
        out["runs"][name] = {"final": curve[-1][1], "multiple": curve[-1][1] / paid, "mwr": xirr(flows),
                             "worst_fall": worst_fall(curve),
                             "marks": {m: [v for d, v in curve if d <= m][-1] for m in marks}}
    out["paid_by"] = {m: 10_000 + 500 * sum(1 for d, f in runs["SPY buy and hold"][1][1:-1] if d <= m) for m in marks}
    return out


# ----------------------------------------------------------------------
# 8. The Nasdaq


def nasdaq(fits_sp: dict, sp_rows, closes, days) -> dict:
    """Part A: QQQ, QLD and TQQQ from the first session with a 250-day average (March 2000). The index return is
    QQQ's total return plus its fee; QLD's and TQQQ's costs are fitted like SSO's. Part B, rough: the Nasdaq
    Composite, price only, 1971-2026, real 30-year periods, with QLD's and TQQQ's fitted costs."""
    d = lt.data()
    nd = d["nasdaq"]
    rate_on = tbill_on()
    annual = d["shiller_rate"]

    def tr_rows(prices, dividends):
        ds = [k for k in sorted(prices) if k <= lt.END]
        return ds, {b: (prices[b] + dividends.get(b, 0.0)) / prices[a] - 1 for a, b in zip(ds, ds[1:])}

    qdays, qtr = tr_rows(nd["etfs"]["QQQ"], nd["dividends"]["QQQ"])
    gaps = {b: (date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(qdays, qdays[1:])}
    qfee = d["fees"]["QQQ"]
    index_ret = {k: qtr[k] + qfee / 100 * gaps[k] / 365 for k in qtr}

    fits = {}
    for sym, lev in (("QLD", 2), ("TQQQ", 3)):
        fdays, ftr = tr_rows(nd["etfs"][sym], nd["dividends"][sym])
        ds = [k for k in fdays[1:] if k in index_ret]
        years = (date.fromisoformat(ds[-1]) - date.fromisoformat(fdays[0])).days / 365.25
        actual = math.prod(1 + ftr[k] for k in ds)
        fee = d["fees"][sym]
        lo, hi = -3.0, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            model = math.prod(1 + lev * index_ret[k] - (lev - 1) * (rate_on(k) + mid) / 100 * gaps[k] / 365
                              - fee / 100 * gaps[k] / 365 for k in ds)
            lo, hi = (mid, hi) if model > actual else (lo, mid)
        spread = (lo + hi) / 2
        fits[sym] = {"lev": lev, "fee": fee, "spread": spread, "all_in": fee + (lev - 1) * spread,
                     "cagr": 100 * (actual ** (1 / years) - 1), "since": fdays[0]}

    def sma_signals(cl, ds, length, monthly):
        out, current = {}, None
        csum = np.cumsum([cl[k] for k in ds])
        for i, day in enumerate(ds):
            if i + 1 >= length:
                sma = (csum[i] - (csum[i - length] if i >= length else 0)) / length
                month_end = i + 1 == len(ds) or ds[i + 1][:7] != day[:7]
                if not monthly or month_end or current is None:
                    current = cl[day] > sma
            out[day] = current
        return out

    def run(ds, gp, ret_of, cash_of, signal, lev, fee, spread, start):
        eq, out, prev, switches = 1.0, [], None, 0
        for i, day in enumerate(ds):
            if day < start or day > lt.END or i < 2:
                continue
            held = True if signal is None else bool(signal[ds[i - 2]])
            r = ret_of(day)
            daily = (lev * r - (lev - 1) * (cash_of(day) + spread) / 100 * gp[day] / 365 - fee / 100 * gp[day] / 365) \
                if held else cash_of(day) / 100 * gp[day] / 365
            if prev is not None and held != prev:
                daily -= lt.SWITCH_COST
                switches += 1
            eq *= 1 + daily
            out.append((day, eq))
            prev = held
        return out, switches

    def mix(c2, c3):
        v2, v3, out = 0.75, 0.25, []
        for k, ((day, e2), (_, e3)) in enumerate(zip(c2, c3)):
            if k:
                v2 *= e2 / c2[k - 1][1]
                v3 *= e3 / c3[k - 1][1]
                if k + 1 < len(c2) and c2[k + 1][0][:4] != day[:4]:
                    total = v2 + v3
                    v2, v3 = 0.75 * total, 0.25 * total
            out.append((day, v2 + v3))
        return out

    def summary(curve, switches=0):
        s = lt.stats([(k, v, True) for k, v in curve], switches)
        return {"end": CAPITAL * curve[-1][1] / curve[0][1], "cagr": s["cagr"], "maxdd": s["maxdd"],
                "dd_when": s["dd_when"], "worst_year": s["worst_year"], "underwater_years": s["underwater_years"],
                "switches_per_year": s["switches_per_year"]}

    def windows(vals, ds, years, first):
        starts = [k for i, k in enumerate(ds) if i and k[:7] != ds[i - 1][:7] and k >= first]
        out, used = {name: [] for name in vals}, []
        for s in starts:
            target = (date.fromisoformat(s) + timedelta(days=round(365.25 * years))).isoformat()
            if target > lt.END:
                break
            e = ds[bisect_right(ds, target) - 1]
            used.append(s)
            for name, c in vals.items():
                out[name].append(CAPITAL * c[e] / c[s])
        return used, {k: np.array(v) for k, v in out.items()}

    # Part A
    qcloses = {k: nd["etfs"]["QQQ"][k] for k in qdays}
    a0 = qdays[250]
    sd, str_ = tr_rows(d["etfs"]["SPY"], d["etf_dividends"]["SPY"])
    curves, switch_count = {}, {}
    curves["QQQ buy and hold"], _ = run(qdays, gaps, lambda k: qtr[k], rate_on, None, 1, 0.0, 0.0, a0)
    curves["SPY buy and hold"], _ = run(qdays, gaps, lambda k: str_[k], rate_on, None, 1, 0.0, 0.0, a0)
    rules = {"monthly 200-day": (200, True), "monthly 250-day": (250, True), "daily 200-day": (200, False)}
    for rule, (n, monthly) in rules.items():
        sig = sma_signals(qcloses, qdays, n, monthly)
        for sym in ("QLD", "TQQQ"):
            f = fits[sym]
            key = f"{sym} {f['lev']}x, {rule}"
            curves[key], switch_count[key] = run(qdays, gaps, lambda k: index_ret[k], rate_on, sig, f["lev"], f["fee"],
                                                 f["spread"], a0)
        curves[f"Nasdaq mix 75/25, {rule}"] = mix(curves[f"QLD 2x, {rule}"], curves[f"TQQQ 3x, {rule}"])
        switch_count[f"Nasdaq mix 75/25, {rule}"] = switch_count[f"QLD 2x, {rule}"]
    sp = {}
    sig = lt.signals(closes, days, True)
    for name, fund in (("S&P 2x (SSO), monthly 200-day", "SSO"), ("S&P 3x (UPRO), monthly 200-day", "UPRO")):
        c, _ = lt.simulate(sp_rows, sig, fits_sp[fund]["lev"], fee=fits_sp[fund]["fee"], spread=fits_sp[fund]["spread"])
        vals = {k: v for k, v, _ in c}
        sp[name] = [(k, vals[k]) for k in qdays if a0 <= k <= lt.END and k in vals]
    sp["S&P mix 75/25, monthly 200-day"] = mix(sp["S&P 2x (SSO), monthly 200-day"], sp["S&P 3x (UPRO), monthly 200-day"])
    curves.update(sp)
    part_a = {name: summary(c, switch_count.get(name, 0)) for name, c in curves.items()}
    vals = {name: {k: v for k, v in c} for name, c in curves.items()}
    common = [k for k in qdays if a0 <= k <= lt.END and all(k in v for v in vals.values())]
    rolling_a = {}
    for years in (10, 20):
        used, res = windows(vals, common, years, a0)
        rolling_a[str(years)] = {"starts": [used[0], used[-1], len(used)], "rows": {
            name: {"worst": float(e.min()), "median": float(np.median(e)), "p10": float(np.percentile(e, 10)),
                   "beat_qqq": 100 * float((e > res["QQQ buy and hold"]).mean()),
                   "beat_spy": 100 * float((e > res["SPY buy and hold"]).mean()),
                   "beat_sp2x": 100 * float((e > res["S&P 2x (SSO), monthly 200-day"]).mean()),
                   "beat_sp3x": 100 * float((e > res["S&P 3x (UPRO), monthly 200-day"]).mean()),
                   "beat_spmix": 100 * float((e > res["S&P mix 75/25, monthly 200-day"]).mean())}
            for name, e in res.items()}}

    # Part B
    ix = {k: v for k, v in nd["ixic"].items() if k <= lt.END}
    bdays = sorted(ix)
    bgaps = {b: (date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(bdays, bdays[1:])}
    cash_b = lambda k: rate_on(k) if k >= "1990-01-02" else annual[k[:4]]  # noqa: E731
    ret_b = {b: ix[b] / ix[a] - 1 for a, b in zip(bdays, bdays[1:])}
    b0 = bdays[251]
    bc = {"Composite buy and hold": run(bdays, bgaps, lambda k: ret_b[k], cash_b, None, 1, 0.0, 0.0, b0)[0]}
    for rule, (n, monthly) in rules.items():
        sig = sma_signals(ix, bdays, n, monthly)
        for sym in ("QLD", "TQQQ"):
            f = fits[sym]
            bc[f"{f['lev']}x, {rule}"] = run(bdays, bgaps, lambda k: ret_b[k], cash_b, sig, f["lev"], f["fee"],
                                             f["spread"], b0)[0]
        bc[f"Mix 75/25, {rule}"] = mix(bc[f"2x, {rule}"], bc[f"3x, {rule}"])
    bvals = {k: {x: v for x, v in c} for k, c in bc.items()}
    used, res = windows(bvals, [k for k in bdays if b0 <= k <= lt.END], 30, b0)
    part_b = {"starts": [used[0], used[-1], len(used)], "rows": {}}
    for name, e in res.items():
        s = summary(bc[name])
        part_b["rows"][name] = {"worst": float(e.min()), "p10": float(np.percentile(e, 10)), "median": float(np.median(e)),
                                "p90": float(np.percentile(e, 90)),
                                "beat_hold": 100 * float((e > res["Composite buy and hold"]).mean()),
                                "maxdd": s["maxdd"], "dd_year": s["dd_when"][1][:4]}
    return {"fits": fits, "qqq_fee": qfee, "start": a0, "part_a": part_a, "rolling": rolling_a, "part_b": part_b}


# ----------------------------------------------------------------------
# Main


def main() -> None:
    closes, days, rows = lt.index_series()
    res = {"data_through": lt.END, "capital": CAPITAL,
           "costs": {"spread": lt.SPREAD, "fee_1x": lt.FEE_1X, "fee_lev": lt.FEE_LEV, "switch": lt.SWITCH_COST,
                     "margin_spread": MARGIN_SPREAD, "margin_floor": MARGIN_FLOOR}}
    res["main"], variants = registered(closes, days, rows)
    res["crash_1987"] = crash_1987(closes, days)
    print("registered: done", flush=True)
    res["fits"] = fund_costs(rows)
    res["validation"] = validation(rows)
    print("validation: done", flush=True)
    res["robustness"] = robustness(closes, days, rows)
    res["modern"] = modern(closes, days)
    print("robustness and 1995 on: done", flush=True)
    res["rolling"], res["notable_starts"], table = rolling(closes, days, rows, res["fits"])
    print("30-year periods: done", flush=True)
    res["standard"], curves = standard(closes, days, res["fits"])
    print("reporting standard: done", flush=True)
    res["dca"] = dca(closes, days, res["fits"])
    res["nasdaq"] = nasdaq(res["fits"], rows, closes, days)
    print("Nasdaq: done", flush=True)
    res["signal"] = signal_now(closes, days)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    with (OUT / "rolling_30y.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        names = [k for k in table[0] if k != "start"]
        w.writerow(["start"] + names)
        for row in table:
            w.writerow([row["start"]] + [round(row[k], 2) for k in names])
    with (OUT / "curves_full.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        names = [k for k in curves if k != "dates"]
        w.writerow(["date"] + names)
        for i, d in enumerate(curves["dates"]):
            w.writerow([d] + [round(float(curves[k][i]), 2) for k in names])
    switches_csv(closes, days)
    write_report(res)


def signal_now(closes, days) -> dict:
    last = days[-1]
    sma = sum(closes[d] for d in days[-200:]) / 200
    return {"day": last, "close": closes[last], "sma200": sma, "above_pct": 100 * (closes[last] / sma - 1)}


def switches_csv(closes, days) -> None:
    """Every switch of the 2x month-end and daily rules: the decision close and the close it fills at."""
    with (OUT / "switches.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rule", "decision_close", "fill_close", "action", "sp500_close", "sma200"])
        for check in ("monthly", "daily"):
            sig = lt.signals(closes, days, check == "monthly")
            sma = np.convolve([closes[d] for d in days], np.ones(200) / 200, mode="valid")
            for i in range(1, len(days) - 1):
                a, b = sig[days[i - 1]], sig[days[i]]
                if a is not None and b is not None and a != b and days[i] >= "1949-12-01":
                    w.writerow([f"{check} 200-day", days[i], days[i + 1], "buy 2x" if b else "sell to T-bills",
                                round(closes[days[i]], 2), round(float(sma[i - 199]), 2)])


def write_report(res: dict) -> None:
    import levtrend_report
    (OUT / "report.md").write_text(levtrend_report.report(res), encoding="utf-8")


if __name__ == "__main__":
    if "--report" in sys.argv:
        write_report(json.loads((OUT / "results.json").read_text(encoding="utf-8")))
    else:
        main()
