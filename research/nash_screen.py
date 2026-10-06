"""Tom Nash's quality checklist as a mechanical screen, with and without his "recession-proof" rule.

Rules fixed before any results were seen (2026-09-30):

Universe, at each rebalance: US common stocks that pass the ground-rules liquidity test that day (as-traded
close >= $5, 20-day average dollar volume >= $20M). Financial Services is left out of every run: Nash says not
to own leveraged businesses, and "cash > debt" means nothing for a bank (deposits are not debt).

Fundamentals: FMP quarterly statements, trailing twelve months (TTM) from the latest 8 consecutive quarters.
A quarter is usable from its filing date; where FMP's filing date is within 10 days of the period end (foreign
filers carry the period end), from 45 days after the period end. Data older than 200 days at a rebalance
(no recent filing) makes the stock ineligible.

Checklist (video 3DEAP6gBlVU):
  1  cash and short-term investments > total debt, leases excluded
  2  TTM revenue growth >= 10%
  3  margin >= 15%: operating margin (variant "OM") or free-cash-flow margin (variant "FCF")
  4  TTM revenue growth > TTM operating-expense growth
  7  (optional) no cyclical industry: excludes Energy, Basic Materials, Consumer Cyclical, Real Estate,
     Industrials other than Aerospace & Defense and Waste Management, and the Technology industries
     Semiconductors; Hardware, Equipment & Parts; Computer Hardware; Consumer Electronics;
     Technology Distributors; Communication Equipment.
  5 (moat) and 6 (CEO) have no mechanical version and are not applied.

Portfolio: first session of each quarter, at the close. Holdings that still pass every rule are kept; empty
slots (up to 10) are filled with the passing stocks with the highest Rule of 40 (TTM revenue growth + TTM FCF
margin). Weights reset each quarter:
  "blend"  50% SPY + 5% per stock (Nash's structure); unfilled slots go to SPY.
  "sleeve" 10% per stock; unfilled slots go to SPY. Shows the stock picks on their own.
A delisted holding is sold at its last close; the cash waits for the next rebalance. Dividends are paid in cash
(FMP adjDividend) and reinvested at the next rebalance. Costs: ground-rules slippage on every trade, 0.10%, or
0.25% under $20 as traded. Idle cash earns nothing. Benchmark: SPY with dividends.
Periods: in-sample 2016-2021, out-of-sample 2022 onward, combined 2016 onward; each from $100,000. Nothing is
fitted, so the split only shows whether results hold in both halves.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import benchmarks
import panel as P
from stratlib.app import open_context
from nash_fundamentals import KEY, classifications, rebalance_rows

OUT = Path(__file__).resolve().parent / "output" / "nash_screen"
DIV_KEY = "research:nash:div:"
START_EQUITY = 100_000.0
SLOTS = 10
STALE_DAYS = 200
PERIODS = {"in-sample": ("2016-01-01", "2021-12-31"), "out-of-sample": ("2022-01-01", "9999-12-31"),
           "combined": ("2016-01-01", "9999-12-31")}
CYCLICAL_SECTORS = {"Energy", "Basic Materials", "Consumer Cyclical", "Real Estate", "Industrials"}
INDUSTRIAL_KEEP = {"Aerospace & Defense", "Waste Management"}
CYCLICAL_TECH = {"Semiconductors", "Hardware, Equipment & Parts", "Computer Hardware", "Consumer Electronics",
                 "Technology Distributors", "Communication Equipment"}


def cyclical(sector: str, industry: str) -> bool:
    if sector == "Industrials":
        return industry not in INDUSTRIAL_KEEP
    return sector in CYCLICAL_SECTORS or (sector == "Technology" and industry in CYCLICAL_TECH)


# ----------------------------------------------------------------------------------------------
# Fundamentals

def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def available_from(row: dict) -> str:
    period_end = _d(row["date"])
    filed = _d(row["filingDate"]) if row.get("filingDate") else None
    if filed is None or (filed - period_end).days < 10:
        return (period_end + timedelta(days=45)).isoformat()
    return filed.isoformat()


def snapshots(doc: dict) -> list[tuple[str, str, dict]]:
    """(available date, latest quarter end, metrics) for every TTM window with 8 consecutive quarters."""
    inc = sorted((r for r in doc.get("income", []) if r.get("date")), key=lambda r: r["date"])
    bal = {r["date"]: r for r in doc.get("balance", []) if r.get("date")}
    cfs = {r["date"]: r for r in doc.get("cash", []) if r.get("date")}
    out = []
    for k in range(7, len(inc)):
        window = inc[k - 7:k + 1]                      # oldest .. newest
        gaps = [(_d(b["date"]) - _d(a["date"])).days for a, b in zip(window, window[1:])]
        if any(g < 60 or g > 120 for g in gaps):
            continue
        prior, recent = window[:4], window[4:]

        def total(rows, key):
            vals = [r.get(key) for r in rows]
            return None if any(v is None for v in vals) else float(sum(vals))

        rev, rev0 = total(recent, "revenue"), total(prior, "revenue")
        if not rev or not rev0 or rev <= 0 or rev0 <= 0:
            continue
        opx, opx0 = total(recent, "operatingExpenses"), total(prior, "operatingExpenses")
        oi = total(recent, "operatingIncome")
        fcf_rows = [cfs.get(r["date"]) for r in recent]
        fcf = None if any(r is None or r.get("freeCashFlow") is None for r in fcf_rows) else \
            float(sum(r["freeCashFlow"] for r in fcf_rows))
        b = bal.get(window[-1]["date"]) or {}
        cash, debt = b.get("cashAndShortTermInvestments"), b.get("totalDebt")
        leases = b.get("capitalLeaseObligations") or 0
        if debt is not None and 0 < leases <= debt:
            debt = debt - leases
        m = {
            "rev_g": rev / rev0 - 1,
            "opx_g": (opx / opx0 - 1) if opx is not None and opx0 and opx0 > 0 and opx > 0 else None,
            "om": oi / rev if oi is not None else None,
            "fcfm": fcf / rev if fcf is not None else None,
            "cash_gt_debt": (cash > debt) if cash is not None and debt is not None else None,
        }
        m["r40"] = m["rev_g"] + m["fcfm"] if m["fcfm"] is not None else None
        out.append((available_from(window[-1]), window[-1]["date"], m))
    out.sort(key=lambda x: x[0])
    return out


def passes(m: dict, margin: str) -> bool:
    if not m["cash_gt_debt"] or m["rev_g"] < 0.10:
        return False
    margin_value = m["om"] if margin == "OM" else m["fcfm"]
    if margin_value is None or margin_value < 0.15:
        return False
    return m["opx_g"] is not None and m["rev_g"] > m["opx_g"]


def latest(snaps, day: str):
    """Most recent snapshot available strictly before `day`, if not stale."""
    best = None
    for avail, qend, m in snaps:
        if avail < day:
            best = (qend, m)
        else:
            break
    if best is None or (_d(day) - _d(best[0])).days > STALE_DAYS:
        return None
    return best


# ----------------------------------------------------------------------------------------------
# Selection

def select_all(p, snaps, classes, margin: str, rule7: bool) -> dict[int, dict]:
    """Holdings chosen at each rebalance session (sticky), with screen counts."""
    out, held = {}, []
    stock_cols = np.flatnonzero(p.kind == "stock")
    for i in rebalance_rows(p):
        day = str(p.dates[i])
        passing = {}
        for j in stock_cols:
            s = str(p.symbols[j])
            if s not in snaps or not p.eligible[i, j]:
                continue
            sector, industry = classes.get(s, ("", ""))
            if sector == "Financial Services" or (rule7 and cyclical(sector, industry)):
                continue
            snap = latest(snaps[s], day)
            if snap and passes(snap[1], margin):
                passing[s] = snap[1]
        kept = [s for s in held if s in passing]
        ranked = sorted((s for s in passing if s not in kept),
                        key=lambda s: -(passing[s]["r40"] if passing[s]["r40"] is not None else -9))
        held = kept + ranked[:SLOTS - len(kept)]
        out[i] = {"day": day, "passing": len(passing), "held": list(held),
                  "metrics": {s: passing[s] for s in held}}
    return out


# ----------------------------------------------------------------------------------------------
# Simulation

def slippage(as_traded: float) -> float:
    return 0.0025 if as_traded < 20 else 0.0010


def simulate(p, selections, dividends, first: str, last: str, stock_weight: float) -> dict:
    rows = [i for i in range(len(p.dates)) if first <= p.dates[i] <= last]
    spy = p.index["SPY"]
    shares: dict[int, float] = {}
    last_px: dict[int, float] = {}
    cash, costs, turnover, curve, names = START_EQUITY, 0.0, 0.0, [], []
    started = False

    def price(j, i):
        v = p.close[i, j]
        if math.isfinite(v) and v > 0:
            last_px[j] = v
        return last_px.get(j)

    for i in rows:
        day = str(p.dates[i])
        # Dividends (ex-date) on shares held into the day.
        for j, n in shares.items():
            d = dividends.get(str(p.symbols[j]), {}).get(day)
            if d:
                cash += n * d
        # A holding whose prices end (delisted, acquired) leaves at its last close.
        for j in [j for j in shares if p.last_bar[j] <= i < len(p.dates) - 1 and j != spy]:
            px = price(j, i)
            value = shares.pop(j) * px
            fee = value * slippage(px * p.factor[min(i, p.last_bar[j]), j])
            cash += value - fee
            costs += fee
        if i in selections:
            started = True
            equity = cash + sum(n * price(j, i) for j, n in shares.items())
            sel = [p.index[s] for s in selections[i]["held"]]
            target = {j: stock_weight for j in sel}
            target[spy] = target.get(spy, 0) + 1 - stock_weight * len(sel)
            trade_value = 0.0
            for j in set(target) | set(shares):
                px = price(j, i)
                want = target.get(j, 0) * equity / px
                delta = want - shares.get(j, 0.0)
                if abs(delta) * px < 1e-6:
                    continue
                fee = abs(delta) * px * slippage(px * p.factor[i, j])
                cash -= delta * px + fee
                costs += fee
                trade_value += abs(delta) * px
                if want > 0:
                    shares[j] = want
                else:
                    shares.pop(j, None)
            turnover += trade_value / equity
            names.append(len(sel))
        if not started:
            continue
        equity = cash + sum(n * price(j, i) for j, n in shares.items())
        curve.append((day, equity))
    return {"curve": curve, "costs": costs, "turnover": turnover, "names": names}


def spy_curve(p, dividends, first, last):
    spy = p.index["SPY"]
    rows = [i for i in range(len(p.dates)) if first <= p.dates[i] <= last]
    n = START_EQUITY / p.close[rows[0], spy]
    cash, curve = 0.0, []
    for k, i in enumerate(rows):
        d = dividends.get(str(p.dates[i]))
        if d and k:
            n += n * d / p.close[i, spy]          # reinvested at the ex-date close
        curve.append((str(p.dates[i]), n * p.close[i, spy]))
    return curve


def stats(curve, tbill):
    days = [d for d, _ in curve]
    v = np.array([x for _, x in curve])
    years = (_d(days[-1]) - _d(days[0])).days / 365.25
    r = v[1:] / v[:-1] - 1
    rf = np.array([tbill.get(d, 0.0) for d in days[1:]]) / 100 / 252
    peak = np.maximum.accumulate(v)
    yearly, prev = {}, v[0]
    for k, d in enumerate(days):
        if k + 1 == len(days) or days[k + 1][:4] != d[:4]:
            yearly[d[:4]] = 100 * (v[k] / prev - 1)
            prev = v[k]
    return {"cagr": 100 * ((v[-1] / v[0]) ** (1 / years) - 1), "total": 100 * (v[-1] / v[0] - 1),
            "maxdd": 100 * (1 - v / peak).max(), "vol": 100 * r.std() * math.sqrt(252),
            "sharpe": (r - rf).mean() / (r - rf).std() * math.sqrt(252), "years": years, "yearly": yearly}


# ----------------------------------------------------------------------------------------------

def load_dividends(ctx, symbols) -> dict[str, dict[str, float]]:
    out = {}
    for s in sorted(symbols):
        doc = ctx.store.document(DIV_KEY + s)
        if not doc:
            try:
                rows = ctx.client.get("dividends", {"symbol": s})
            except Exception as exc:          # a missing history means no dividends are credited
                rows, doc = [], {"error": str(exc)}
            doc = {"rows": {r["date"]: float(r.get("adjDividend") or 0) for r in rows or [] if r.get("date")},
                   **({"error": doc["error"]} if doc and "error" in doc else {})}
            ctx.store.save_document(DIV_KEY + s, doc)
        out[s] = doc["rows"]
    return out


def main() -> None:
    p = P.load()
    bench = benchmarks.load()
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        snaps = {}
        for key in ctx.store.document_keys(KEY):
            doc = ctx.store.document(key)
            if doc and "error" not in doc:
                snaps[key[len(KEY):]] = snapshots(doc)
        print(f"Statement histories: {len(snaps):,}")
        variants = {(m, r7): select_all(p, snaps, classes, m, r7) for m in ("OM", "FCF") for r7 in (False, True)}
        held = {s for sel in variants.values() for x in sel.values() for s in x["held"]}
        print(f"Distinct stocks ever held: {len(held)}; fetching dividends")
        divs = load_dividends(ctx, held)
    finally:
        ctx.close()
    divs["SPY"] = bench["spy_dividends"]
    tbill = bench["tbill3m"]

    OUT.mkdir(parents=True, exist_ok=True)
    results = {"rules": __doc__, "runs": {}, "benchmark": {}, "selections": {}}
    for period, (a, b) in PERIODS.items():
        results["benchmark"][period] = stats(spy_curve(p, divs["SPY"], a, b), tbill)
    for (margin, r7), sel in variants.items():
        name = f"{margin}{'+rule7' if r7 else ''}"
        results["selections"][name] = [{"day": x["day"], "passing": x["passing"], "held": x["held"],
                                        "r40": {s: round(100 * (m["r40"] or 0), 1) for s, m in x["metrics"].items()}}
                                       for x in sel.values()]
        for form, weight in (("blend", 0.05), ("sleeve", 0.10)):
            for period, (a, b) in PERIODS.items():
                sub = {i: x for i, x in sel.items() if a <= x["day"] <= b}
                run = simulate(p, sub, divs, a, b, weight)
                s = stats(run["curve"], tbill)
                s.update({"costs": run["costs"], "turnover_per_year": run["turnover"] / s["years"],
                          "avg_names": float(np.mean(run["names"]))})
                results["runs"][f"{name} {form} {period}"] = s
                with open(OUT / f"curve_{name}_{form}_{period}.csv", "w", newline="") as f:
                    csv.writer(f).writerows(run["curve"])
    json.dump(results, open(OUT / "results.json", "w"), indent=1, default=float)

    print(f"\n{'run':34}{'CAGR':>7}{'SPY':>7}{'excess':>8}{'maxDD':>7}{'SPY DD':>7}{'Sharpe':>7}{'SPY':>6}"
          f"{'names':>6}{'turn/yr':>8}")
    for key, s in results["runs"].items():
        period = key.split(" ", 2)[2]
        bm = results["benchmark"][period]
        print(f"{key:34}{s['cagr']:6.1f}%{bm['cagr']:6.1f}%{s['cagr'] - bm['cagr']:+7.1f}%{s['maxdd']:6.1f}%"
              f"{bm['maxdd']:6.1f}%{s['sharpe']:7.2f}{bm['sharpe']:6.2f}{s['avg_names']:6.1f}{100 * s['turnover_per_year']:7.0f}%")


if __name__ == "__main__":
    main()
