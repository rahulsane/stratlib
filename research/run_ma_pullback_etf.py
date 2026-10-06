"""The MA pullback on SPY and QQQ, under the reporting standard of 2026-10-02. Findings: output/ma_pullback_etf/report.md.

The rules and the eight variants are those of run_ma_pullback.py (src/stratlib/sim/strategies/ma_pullback.py), on the two
index ETFs instead of stocks (an exception to the standard's stocks-only rule, at the user's request), from their first
trading days (ma_pullback_etf_data.py): SPY from 1993, QQQ from 1999.

Touch rules:
  third      as specified: only the third pullback after two completed tests (the rule fixed for the stock study)
  third+     the third or any later pullback (Rayner's "a minimum of two tests"), a departure
  any        every pullback into the area, a departure: the bounce without the test count
The two departures were added after counting signals (13 in 33 years for the rule as specified) and are reported
apart from it.
Size: 1% of equity at risk (his rule), no cap but the cash available (two diversified ETFs, at most two positions),
no margin. Robustness adds half the account per position.
Periods: 1994 on (the main one: SPY's averages need 1993 to form; QQQ trades from 2000), 2000 on (both ETFs), 2016 on
(the standard's window), and 1994-2015 / 2016 on run separately. Benchmarks: SPY (the standard), QQQ from 2000 for
context. Random selection does not apply (at most two candidates).
Event study: forward returns after each signal against every session that passes the same trend filter.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_ma_pullback_etf.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_ma_pullback_etf.py --report
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from itertools import product

import numpy as np

import ma_pullback_etf_data as D
import run_ma_pullback as R
import run_wtt_standard as S
from stratlib.sim.panel import Panel
from engine import OUTPUT, Rules, simulate
from strategies.ma_pullback import MaPullback
from tax_accounting import NO_TAX, Accountant, TaxRates

OUT = OUTPUT / "ma_pullback_etf"
WORKERS = 6
RATES = TaxRates()
RULES = Rules(risk_pct=1.0, max_position_pct=100.0, max_positions=2)
CAPITAL = RULES.capital
VARIANTS = R.VARIANTS
TOUCHES = {"third": ({"touch": 3, "touch_rule": "exact"}, "Third touch only (as specified)"),
           "third+": ({"touch": 3, "touch_rule": "at least"}, "Third touch or later (departure)"),
           "any": ({"touch": 1, "touch_rule": "at least"}, "Any touch (departure)")}
UNIVERSES = {"both": "SPY and QQQ", "SPY": "SPY only", "QQQ": "QQQ only"}
PERIODS = {"long": ("1994-01-03", None), "since_2000": ("2000-01-03", None), "combined": ("2016-01-01", None),
           "early": ("1994-01-03", "2015-12-31")}
PERIOD_TITLES = {"long": "1994 on", "since_2000": "2000 on", "combined": "2016 on", "early": "1994–2015"}
HORIZONS = (5, 10, 20, 60)


def neighbours(v: dict) -> dict:
    out = R.neighbours(v)
    out["half the account per position"] = ({"position_pct": 50.0}, None)
    return out


# ----------------------------------------------------------------------
# Data and runs

_W: dict = {}


def _init() -> None:
    d = D.load()
    p = d["panel"]
    n = len(p.dates)
    MaPullback.members = {"both": np.ones((n, 2), dtype=bool),
                          "SPY": np.tile([True, False], (n, 1)), "QQQ": np.tile([False, True], (n, 1))}
    rates = np.array([d["tbill3m"].get(str(x), np.nan) for x in p.dates], dtype=float)
    tbill = np.nan_to_num(S._ffill(rates))
    j = p.index["QQQ"]
    qqq = Panel(dates=p.dates, symbols=np.array(["SPY"]), kind=np.array(["etf"]), until=np.array([""]),
                open=p.open[:, [j]], high=p.high[:, [j]], low=p.low[:, [j]], close=p.close[:, [j]],
                volume=p.volume[:, [j]], factor=p.factor[:, [j]], min_price=1.0, min_dollar_volume=0.0)
    _W.update(panel=p, divs=d["dividends"], tbill=tbill, qqq_panel=qqq, qqq_divs=d["qqq_dividends"],
              spy_divs=d["spy_dividends"], tbill3m=d["tbill3m"])
    S._W.update(panel=p, bench={"spy_dividends": d["spy_dividends"], "tbill3m": d["tbill3m"]}, tbill=tbill)
    R._W.update(panel=p)


def hold_account(symbol: str, start: str, end: str | None, leverage: float, taxed: bool) -> dict:
    """Buy and hold (or held at `leverage`) under the same taxes: run_wtt_standard.spy_account on SPY or QQQ."""
    if symbol == "SPY":
        return S.spy_account(start, end, leverage, taxed)
    saved = dict(S._W)
    S._W.update(panel=_W["qqq_panel"], bench={"spy_dividends": _W["qqq_divs"]})
    try:
        return S.spy_account(start, end, leverage, taxed)
    finally:
        S._W.update(saved)


def run(variant: str, touch: str, universe: str, period: str, taxed: bool, extra: dict | None = None,
        rules_change: dict | None = None) -> tuple[dict, MaPullback]:
    p = _W["panel"]
    s = MaPullback()
    s.setup(p, {**VARIANTS[variant], **TOUCHES[touch][0], "universe": universe, "kind": "etf", **(extra or {})})
    start, end = PERIODS[period]
    acc = Accountant(p, RATES if taxed else NO_TAX, _W["divs"])
    return simulate(p, s, start, end, replace(RULES, **(rules_change or {})), accountant=acc), s


def fixed_task(job: tuple) -> dict:
    key, variant, touch, universe, period, taxed, extra, change = job
    r, s = run(variant, touch, universe, period, taxed, extra, change)
    y, v = S.years(r), S.terminal(r)
    pnl = sorted((t.pnl for t in r["trades"]), reverse=True)
    out = {"key": key, "taxed": taxed, "period": period, "start": r["start"], "end": r["end"], "years": y, "values": v,
           "cagr_held": S.cagr_of(v["held"], y), "cagr_sold": S.cagr_of(v["sold"], y),
           "cagr_without_top5": S.cagr_of(v["marked"] - sum(pnl[:5]), y), "counts": r["counts"],
           "tax": r["tax"], "taxes_paid": r["taxes_paid"]}
    if extra is None and change is None:
        out.update(dates=[str(d) for d in r["dates"]], equity=r["equity"].tolist(),
                   invested=(r["invested"] / r["equity"]).tolist(), held=r["held"].tolist(),
                   trades=R.trade_rows(r["trades"]))
    return out


# ----------------------------------------------------------------------
# Event study


def event_study() -> dict:
    """Forward returns from the next open to the close h sessions later, after each signal (first trigger of a
    pullback) and after every session passing the same trend filter, per touch rule, trend filter and ETF."""
    p = _W["panel"]
    out = {}
    for touch, trend in product(TOUCHES, ("above", "rising")):
        s = MaPullback()
        s.setup(p, {**VARIANTS[f"target-swing-{trend}"], **TOUCHES[touch][0], "universe": "both", "kind": "etf"})
        with np.errstate(invalid="ignore"):
            base = p.close > s.ind["ema_trend"]
            if trend == "rising":
                lag = np.full_like(s.ind["ema_trend"], np.nan)
                lag[20:] = s.ind["ema_trend"][:-20]
                base &= s.ind["ema_trend"] > lag
        for j, sym in enumerate(p.symbols):
            first = p.session_index("1994-01-03")
            row = {}
            for h in HORIZONS:
                fwd = np.full(len(p.dates), np.nan)
                o, c = p.open[:, j], p.close[:, j]
                t = np.arange(len(p.dates) - h)
                with np.errstate(invalid="ignore"):
                    fwd[t] = 100 * (c[t + h] / o[t + 1] - 1)     # bought at the next open, h sessions held
                valid = np.isfinite(fwd) & (np.arange(len(p.dates)) >= first)
                sig = fwd[valid & s.signals[:, j]]
                all_ = fwd[valid & base[:, j]]
                if len(sig) < 2:
                    row[h] = {"n": int(len(sig)), "mean": float(sig.mean()) if len(sig) else float("nan"),
                              "base_mean": float(all_.mean()), "base_n": int(len(all_))}
                    continue
                se = math.sqrt(sig.var(ddof=1) / len(sig) + all_.var(ddof=1) / len(all_))
                row[h] = {"n": int(len(sig)), "mean": float(sig.mean()), "median": float(np.median(sig)),
                          "positive": 100 * float((sig > 0).mean()), "base_mean": float(all_.mean()),
                          "base_positive": 100 * float((all_ > 0).mean()), "base_n": int(len(all_)),
                          "diff": float(sig.mean() - all_.mean()), "t": float((sig.mean() - all_.mean()) / se)}
            out[f"{touch}/{trend}/{sym}"] = row
    return out


# ----------------------------------------------------------------------
# Main


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    _init()
    p = _W["panel"]
    jobs = []
    for v, touch, taxed in product(VARIANTS, TOUCHES, (False, True)):
        for period in PERIODS:
            jobs.append((f"{v}/{touch}/both/{period}", v, touch, "both", period, taxed, None, None))
        for u in ("SPY", "QQQ"):
            jobs.append((f"{v}/{touch}/{u}/long", v, touch, u, "long", taxed, None, None))
        if touch == "any":
            for label, (extra, change) in neighbours(VARIANTS[v]).items():
                jobs.append((f"{v}/{touch}/both/long/{label}", v, touch, "both", "long", taxed, extra, change))
    jobs.sort(key=lambda j: json.dumps({k: (j[6] or {}).get(k) for k in ("ema_area", "band_atr")}))
    with ProcessPoolExecutor(WORKERS, initializer=_init) as pool:
        fixed = list(pool.map(fixed_task, jobs, chunksize=4))
    print(f"runs: {len(fixed)}, {time.time() - t0:.0f} s", flush=True)
    runs = {(r["key"], r["taxed"]): r for r in fixed}

    bench = {}
    for period, (start, end) in PERIODS.items():
        for taxed in (False, True):
            bench[("SPY", period, taxed)] = hold_account("SPY", start, end, 1.0, taxed)
            if period != "long" and period != "early":
                bench[("QQQ", period, taxed)] = hold_account("QQQ", start, end, 1.0, taxed)
    sp, sa = bench[("SPY", "long", False)], bench[("SPY", "long", True)]
    spy_vol = S.block(sp["dates"], np.array(sp["equity"]), np.array(sp["equity"]))["vol"]
    results = {"data_through": str(p.dates[-1]), "rates": asdict(RATES), "rules": asdict(RULES),
               "margin": {"spread": S.MARGIN_SPREAD, "floor": S.MARGIN_FLOOR,
                          "today": S.margin_rate(float(_W["tbill"][-1])),
                          "max": max(S.margin_rate(float(x)) for x in _W["tbill"][p.session_index("1994-01-03"):])},
               "bench": {f"{k[0]}/{k[1]}/{'after' if k[2] else 'pre'}": {x: v for x, v in b.items()
                                                                           if x not in ("dates", "equity")}
                         for k, b in bench.items()},
               "spy_block": {"pre": S.block(sp["dates"], np.array(sp["equity"]), np.array(sp["equity"])),
                             "after": S.block(sa["dates"], np.array(sa["equity"]), np.array(sa["equity"]))},
               "configs": {}, "robust": {}, "events": event_study()}
    curves = {"date": sp["dates"], "spy_pre": sp["equity"], "spy_after": sa["equity"]}
    for v, touch in product(VARIANTS, TOUCHES):
        name = f"{v}/{touch}"
        cfg = {"periods": {}, "instruments": {}}
        for period in PERIODS:
            pre, post = runs[(f"{name}/both/{period}", False)], runs[(f"{name}/both/{period}", True)]
            bp, ba = bench[("SPY", period, False)], bench[("SPY", period, True)]
            cfg["periods"][period] = {
                "pre": pre["cagr_held"], "after_held": post["cagr_held"], "after_sold": post["cagr_sold"],
                "max_drawdown": S.block(bp["dates"], np.array(pre["equity"]), np.array(bp["equity"]))["max_drawdown"],
                "invested": 100 * float(np.mean(pre["invested"])),
                "trades": R.trade_stats(pre["trades"], pre["years"], with_paths=False) if pre["trades"] else {"trades": 0},
                "spy_pre": bp["cagr_held"], "spy_after_held": ba["cagr_held"], "spy_after_sold": ba["cagr_sold"]}
            if ("QQQ", period, False) in bench:
                cfg["periods"][period]["qqq_pre"] = bench[("QQQ", period, False)]["cagr_held"]
                cfg["periods"][period]["qqq_after_held"] = bench[("QQQ", period, True)]["cagr_held"]
        for u in ("SPY", "QQQ"):
            r = runs[(f"{name}/{u}/long", False)]
            cfg["instruments"][u] = {"cagr": r["cagr_held"], "invested": 100 * float(np.mean(r["invested"])),
                                     "trades": R.trade_stats(r["trades"], r["years"], with_paths=False)
                                     if r["trades"] else {"trades": 0}}
        pre, post = runs[(f"{name}/both/long", False)], runs[(f"{name}/both/long", True)]
        eq_pre, eq_post = np.array(pre["equity"]), np.array(post["equity"])
        cfg["pre"] = S.block(sp["dates"], eq_pre, np.array(sp["equity"]))
        cfg["after"] = S.block(sa["dates"], eq_post, np.array(sa["equity"]))
        cfg["tax"] = S.tax_stats(post)
        cfg["values"] = {"pre": pre["values"], "after": post["values"]}
        cfg["cagr"] = {"pre": pre["cagr_held"], "after_held": post["cagr_held"], "after_sold": post["cagr_sold"]}
        cfg["without_top5"] = pre["cagr_without_top5"]
        cfg["trades"] = R.trade_stats(pre["trades"], pre["years"]) if pre["trades"] else {"trades": 0}
        if pre["trades"]:
            ts = cfg["trades"]
            ts["held_rate"] = ts["expectancy_pct"] / ts["hold"] * 252 if ts["hold"] else float("nan")
        cfg["invested"] = 100 * float(np.mean(pre["invested"]))
        cfg["time_in_market"] = 100 * float(np.mean(np.array(pre["held"]) > 0))
        cfg["counts"] = pre["counts"]
        lev = cfg["pre"]["vol"] / spy_vol
        mpre, mpost = hold_account("SPY", *PERIODS["long"], lev, False), hold_account("SPY", *PERIODS["long"], lev, True)
        cfg["leverage_check"] = {"leverage": lev, "pre": mpre["cagr_held"], "after_held": mpost["cagr_held"],
                                 "after_sold": mpost["cagr_sold"],
                                 "max_drawdown": S.block(sp["dates"], np.array(mpre["equity"]),
                                                         np.array(sp["equity"]))["max_drawdown"]}
        results["configs"][name] = cfg
        if touch == "any":
            results["robust"][name] = {label: {"pre": runs[(f"{name}/both/long/{label}", False)]["cagr_held"],
                                               "after_held": runs[(f"{name}/both/long/{label}", True)]["cagr_held"],
                                               "trades": runs[(f"{name}/both/long/{label}", False)]["counts"]["entries"]}
                                       for label in neighbours(VARIANTS[v])}
        curves[f"{v}_{touch}_pre"] = pre["equity"]
        curves[f"{v}_{touch}_after"] = post["equity"]
        if pre["trades"]:
            with (OUT / f"trades_{v}_{touch}.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(pre["trades"][0]))
                w.writeheader()
                w.writerows(pre["trades"])
    with (OUT / "curves_long.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = list(curves)
        w.writerow(cols)
        for i in range(len(curves["date"])):
            w.writerow([curves["date"][i], *(round(float(curves[c][i]), 2) for c in cols[1:])])
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    (OUT / "report.md").write_text(report(results), encoding="utf-8")
    print(f"done in {time.time() - t0:.0f} s")


def report(res: dict) -> str:
    import ma_pullback_etf_report
    return ma_pullback_etf_report.report(res)


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
