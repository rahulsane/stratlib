"""Rayner Teo's moving-average pullback ("MA bounce") under the reporting standard of 2026-10-02.
Findings: output/ma_pullback/report.md. Rules: src/stratlib/sim/strategies/ma_pullback.py (fixed before any results).

Variants: every combination of the three rules his articles disagree on (8), always entering on the third touch:
  trend   "above"  the close above the 200-day EMA;      "rising" also the 200-day EMA rising over 20 sessions
  stop    "swing"  1 ATR below the pullback's low;        "entry"  2 ATR below the entry
  exit    "target" just before the swing high (0.25 ATR); "trail"  the next open after a close below the 50-day EMA
Universes: point-in-time S&P 500 + MidCap 400 (the closest to the Russell 1000 his own stock systems use) and Russell
3000 members at the signal session, as-traded close of $1 or more.
Size: 1% of equity at risk per trade (his rule), at most 20% of equity in one stock and 10 positions, no margin
(positions shrink to the cash available).
Ranking (chosen before the run): 12-month momentum skipping the latest month, highest first. Random selection (SEEDS
runs) is the baseline; the 3-month return is an alternative.
Costs: slippage 0.10% a side, 0.25% under $20. Dividends included; taxes under the standard (tax_accounting.py), paid
from the account; SPY taxed the same way; SPY on margin at the strategy's volatility (run_wtt_standard.spy_account).
Every-signal study: each variant with no portfolio limits (every signal taken at a tiny size), for trade statistics
free of the ranking and the cash limit.
Robustness: doubled costs, entries a session late, the area, the averages and each rule's parameter one notch either
way, the higher-close trigger, the five largest winners removed, 2016-2021 and 2022 onward run separately.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_ma_pullback.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_ma_pullback.py --report
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from datetime import date
from itertools import product

import numpy as np

import benchmarks
import run_wtt_qs as Q
import run_wtt_standard as S
from engine import OUTPUT, PERIODS, Rules, simulate
from strategies.ma_pullback import MaPullback
from tax_accounting import NO_TAX, Accountant, TaxRates

OUT = OUTPUT / "ma_pullback"
SEEDS = 200
WORKERS = 6
RATES = TaxRates()
PRIMARY = "mom12_1"
RULES = Rules(risk_pct=1.0, max_position_pct=20.0, max_positions=10)
ALL_SIGNALS = Rules(risk_pct=0.01, max_position_pct=1e9, max_positions=10**6, allow_margin=True)
CAPITAL = RULES.capital
UNIVERSES = {"sp900": "S&P 500 + MidCap 400", "r3000": "Russell 3000"}
EXITS = {"target": "Target before the swing high", "trail": "Close below the 50-day EMA"}
STOPS = {"swing": "1 ATR below the swing low", "entry": "2 ATR below the entry"}
TRENDS = {"above": "Above the 200-day EMA", "rising": "Above it, and it is rising"}
VARIANTS = {f"{x}-{s}-{t}": {"exit": x, "stop": s, "trend": t} for x, s, t in product(EXITS, STOPS, TRENDS)}


def neighbours(v: dict) -> dict:
    """Robustness label -> (parameter changes, rule changes) for a variant."""
    out = {"double costs": (None, {"slippage_pct": 0.20, "slippage_low_price_pct": 0.50}),
           "entries a session late": ({"entry_delay": 1}, None),
           "area 0.25 ATR either side": ({"band_atr": 0.25}, None),
           "area 0.75 ATR either side": ({"band_atr": 0.75}, None),
           "40-day EMA (area and exit)": ({"ema_area": 40}, None),
           "60-day EMA (area and exit)": ({"ema_area": 60}, None),
           "higher-close trigger": ({"trigger": "higher close"}, None)}
    if v["stop"] == "swing":
        out["stop 0.5 ATR below the low"] = ({"swing_atr": 0.5}, None)
        out["stop 1.5 ATR below the low"] = ({"swing_atr": 1.5}, None)
    else:
        out["stop 1.5 ATR below the entry"] = ({"entry_atr": 1.5}, None)
        out["stop 2.5 ATR below the entry"] = ({"entry_atr": 2.5}, None)
    if v["exit"] == "target":
        out["target at the swing high"] = ({"target_atr": 0.0}, None)
        out["target 0.5 ATR below it"] = ({"target_atr": 0.5}, None)
    if v["trend"] == "rising":
        out["200-day EMA rising over 10 sessions"] = ({"slope_sessions": 10}, None)
        out["200-day EMA rising over 40 sessions"] = ({"slope_sessions": 40}, None)
    return out


# ----------------------------------------------------------------------
# Data and runs

_W: dict = {}


def _init() -> None:
    p, m = Q.load_data()
    b = benchmarks.load()
    MaPullback.members = {k: m[k] for k in ("r3000", "sp500", "sp400")}
    rates = np.array([b["tbill3m"].get(str(d), np.nan) for d in p.dates], dtype=float)
    S._W.update(panel=p, bench=b, tbill=np.nan_to_num(S._ffill(rates)), divs=S.dividend_events(p))
    _W.update(S._W)


def run(variant: str, universe: str, rank: str, seed: int, period: str, taxed: bool, extra: dict | None = None,
        rules: Rules = RULES, rules_change: dict | None = None) -> tuple[dict, MaPullback]:
    p = _W["panel"]
    s = MaPullback()
    s.setup(p, {**VARIANTS[variant], "universe": universe, "rank": rank, "seed": seed, **(extra or {})})
    start, end = PERIODS[period]
    acc = Accountant(p, RATES if taxed else NO_TAX, _W["divs"])
    return simulate(p, s, start, end, replace(rules, **(rules_change or {})), accountant=acc), s


def trade_rows(trades) -> list[dict]:
    return [{"ticker": t.ticker, "signal_date": t.signal_date, "entry_date": t.entry_date, "exit_date": t.exit_date,
             "entry_price": t.entry_price, "entry_price_raw": t.entry_price_raw, "stop": t.stop,
             "exit_price": t.exit_price, "exit_price_as_traded": t.exit_price_as_traded, "exit_reason": t.exit_reason,
             "return_pct": t.return_pct, "r": t.r, "pnl": t.pnl, "holding_days": t.holding_days,
             "holding_sessions": t.holding_sessions, "position_value": t.position_value,
             "cash_limited": t.cash_limited} for t in trades]


def fixed_task(job: tuple) -> dict:
    """One deterministic run: (key, variant, universe, rank, period, taxed, extra params, rule changes)."""
    key, variant, universe, rank, period, taxed, extra, rules_change = job
    r, s = run(variant, universe, rank, 0, period, taxed, extra, RULES, rules_change)
    y, v = S.years(r), S.terminal(r)
    pnl = sorted((t.pnl for t in r["trades"]), reverse=True)
    out = {"key": key, "variant": variant, "universe": universe, "rank": rank, "period": period, "taxed": taxed,
           "start": r["start"], "end": r["end"], "years": y, "values": v,
           "cagr_held": S.cagr_of(v["held"], y), "cagr_sold": S.cagr_of(v["sold"], y),
           "cagr_without_top5": S.cagr_of(v["marked"] - sum(pnl[:5]), y),
           "counts": r["counts"], "tax": r["tax"], "taxes_paid": r["taxes_paid"]}
    if key.endswith(f"/{PRIMARY}/{period}") or extra is None and rules_change is None:
        out.update(dates=[str(d) for d in r["dates"]], equity=r["equity"].tolist(),
                   invested=(r["invested"] / r["equity"]).tolist(), held=r["held"].tolist(),
                   trades=trade_rows(r["trades"]))
    return out


def mc_task(job: tuple) -> dict:
    variant, universe, seed, taxed = job
    r, _ = run(variant, universe, "random", seed, "combined", taxed)
    y, v = S.years(r), S.terminal(r)
    eq = np.concatenate([[CAPITAL], r["equity"]])
    rs = np.array([t.r for t in r["trades"] if np.isfinite(t.r)])
    return {"variant": variant, "universe": universe, "seed": seed, "taxed": taxed,
            "cagr_held": S.cagr_of(v["held"], y), "cagr_sold": S.cagr_of(v["sold"], y),
            "max_drawdown": 100 * float((1 - eq / np.maximum.accumulate(eq)).max()),
            "trades": len(rs), "expectancy_r": float(rs.mean()) if len(rs) else float("nan")}


def every_signal_task(job: tuple) -> dict:
    variant, universe = job
    r, s = run(variant, universe, PRIMARY, 0, "combined", False, None, ALL_SIGNALS)
    return {"variant": variant, "universe": universe, "trades": trade_rows(r["trades"]), "counts": r["counts"]}


# ----------------------------------------------------------------------
# Trade statistics


def slip(price_as_traded: float, rules: Rules = RULES) -> float:
    return (rules.slippage_low_price_pct if price_as_traded < rules.low_price else rules.slippage_pct) / 100


def excursions(trades: list[dict]) -> list[tuple[float, float]]:
    """(MAE, MFE) in R per trade: the lowest low and highest high from the entry session to the exit (the exit
    session counts only for exits during or at the end of the session), against the entry fill."""
    p = _W["panel"]
    out = []
    for t in trades:
        j = p.index[t["ticker"]]
        a, b = p.day[t["entry_date"]], p.day[t["exit_date"]]
        at_open = any(x in t["exit_reason"] for x in ("gap", "EMA", "tax"))
        hi_end = b if not at_open or b == a else b - 1
        lows, highs = p.low[a:hi_end + 1, j], p.high[a:hi_end + 1, j]
        risk = t["entry_price"] - t["stop"]
        lo = np.nanmin(np.append(lows, t["exit_price"]))
        hi = np.nanmax(np.append(highs, t["exit_price"]))
        out.append(((t["entry_price"] - lo) / risk, (hi - t["entry_price"]) / risk))
    return out


def spy_excess(trades: list[dict]) -> np.ndarray:
    """Each trade's return minus SPY's over about the same time: SPY from the entry session's open to the exit
    session's open (exits at the open) or close."""
    p = _W["panel"]
    j = p.index["SPY"]
    out = []
    for t in trades:
        a, b = p.day[t["entry_date"]], p.day[t["exit_date"]]
        at_open = any(x in t["exit_reason"] for x in ("gap", "EMA", "tax"))
        end = p.open[b, j] if at_open else p.close[b, j]
        out.append(t["return_pct"] - 100 * (end / p.open[a, j] - 1))
    return np.array(out)


def trade_stats(trades: list[dict], yrs: float, with_paths: bool = True) -> dict:
    """The short-term trading measures, from trades in exit order."""
    trades = sorted(trades, key=lambda t: (t["exit_date"], t["entry_date"]))
    r = np.array([t["r"] for t in trades], dtype=float)
    ret = np.array([t["return_pct"] for t in trades], dtype=float)
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    hold = np.array([t["holding_sessions"] for t in trades], dtype=float)
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    win = pnl > 0
    risk = np.array([t["entry_price"] - t["stop"] for t in trades])
    cost = np.array([(t["entry_price"] - (t["entry_price_raw"] or t["entry_price"]))
                     + t["exit_price"] * slip(t["exit_price_as_traded"]) / (1 - slip(t["exit_price_as_traded"]))
                     for t in trades]) / risk
    sd = float(r.std(ddof=1)) if n > 1 else float("nan")
    avg_w = float(r[win].mean()) if win.any() else float("nan")
    avg_l = float(r[~win].mean()) if (~win).any() else float("nan")
    payoff = avg_w / -avg_l if (~win).any() and avg_l < 0 else float("nan")
    wr = float(win.mean())
    streak = longest = 0
    for w in win:
        streak = 0 if w else streak + 1
        longest = max(longest, streak)
    reasons: dict[str, int] = {}
    for t in trades:
        key = t["exit_reason"].split(" + ")[-1]
        key = "stop" if key.startswith("stop") else "target" if key.startswith("target") else key
        reasons[key] = reasons.get(key, 0) + 1
    gross_w, gross_l = pnl[win].sum(), -pnl[~win].sum()
    out = {"trades": n, "per_year": n / yrs, "win_rate": 100 * wr,
           "avg_win_pct": float(ret[win].mean()) if win.any() else float("nan"),
           "avg_loss_pct": float(ret[~win].mean()) if (~win).any() else float("nan"),
           "avg_win_r": avg_w, "avg_loss_r": avg_l, "payoff": payoff,
           "expectancy_r": float(r.mean()), "expectancy_se": sd / math.sqrt(n) if n > 1 else float("nan"),
           "t_stat": float(r.mean()) / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan"),
           "sqn": math.sqrt(min(n, 100)) * float(r.mean()) / sd if n > 1 and sd > 0 else float("nan"),
           "expectancy_pct": float(ret.mean()), "expectancy_usd": float(pnl.mean()),
           "median_r": float(np.median(r)), "best_r": float(r.max()), "worst_r": float(r.min()),
           "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
           "kelly": 100 * (wr - (1 - wr) / payoff) if payoff and math.isfinite(payoff) else float("nan"),
           "cost_r": float(cost.mean()), "expectancy_before_costs_r": float((r + cost).mean()),
           "max_losing_streak": longest, "hold": float(hold.mean()), "hold_median": float(np.median(hold)),
           "hold_win": float(hold[win].mean()) if win.any() else float("nan"),
           "hold_loss": float(hold[~win].mean()) if (~win).any() else float("nan"),
           "exits": {k: 100 * v / n for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])}}
    if with_paths:
        ex = np.array(excursions(trades))
        out.update(mae_win=float(ex[win, 0].mean()) if win.any() else float("nan"),
                   mfe_loss=float(ex[~win, 1].mean()) if (~win).any() else float("nan"),
                   losers_up_1r=100 * float((ex[~win, 1] >= 1).mean()) if (~win).any() else float("nan"),
                   mfe=float(ex[:, 1].mean()), mae=float(ex[:, 0].mean()))
        out["spy_excess_pct"] = float(spy_excess(trades).mean())
    return out


# ----------------------------------------------------------------------
# Main


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    _init()
    p = _W["panel"]
    print(f"panel {p.close.shape}, dividend sessions {len(_W['divs'])}", flush=True)
    fixed_jobs = []
    for v, u in product(VARIANTS, UNIVERSES):
        for taxed in (False, True):
            for period in PERIODS:
                fixed_jobs.append((f"{v}/{u}/{PRIMARY}/{period}", v, u, PRIMARY, period, taxed, None, None))
            fixed_jobs.append((f"{v}/{u}/ret63/combined", v, u, "ret63", "combined", taxed, None, None))
            for label, (extra, change) in neighbours(VARIANTS[v]).items():
                fixed_jobs.append((f"{v}/{u}/{label}", v, u, PRIMARY, "combined", taxed, extra, change))
    # group jobs that share indicator settings, so each worker builds few of them
    fixed_jobs.sort(key=lambda j: json.dumps({k: (j[6] or {}).get(k) for k in ("ema_area", "band_atr")}))
    mc_jobs = [(v, u, seed, taxed) for v in VARIANTS for u in UNIVERSES for seed in range(SEEDS)
               for taxed in (False, True)]
    with ProcessPoolExecutor(WORKERS, initializer=_init) as pool:
        every = list(pool.map(every_signal_task, list(product(VARIANTS, UNIVERSES))))
        print(f"every-signal runs: {len(every)}, {time.time() - t0:.0f} s", flush=True)
        fixed = list(pool.map(fixed_task, fixed_jobs, chunksize=4))
        print(f"fixed runs: {len(fixed)}, {time.time() - t0:.0f} s", flush=True)
        mc = list(pool.map(mc_task, mc_jobs, chunksize=16))
    print(f"random runs: {len(mc)}, {time.time() - t0:.0f} s", flush=True)

    runs = {(r["key"], r["taxed"]): r for r in fixed}
    spy = {(period, taxed): S.spy_account(start, end, 1.0, taxed)
           for period, (start, end) in PERIODS.items() for taxed in (False, True)}
    sp, sa = spy[("combined", False)], spy[("combined", True)]
    spy_vol = S.block(sp["dates"], np.array(sp["equity"]), np.array(sp["equity"]))["vol"]
    results = {"data_through": str(p.dates[-1]), "seeds": SEEDS, "rates": asdict(RATES), "rules": asdict(RULES),
               "margin": {"spread": S.MARGIN_SPREAD, "floor": S.MARGIN_FLOOR,
                          "today": S.margin_rate(float(_W["tbill"][-1])),
                          "max": max(S.margin_rate(float(x)) for x in _W["tbill"][p.session_index(PERIODS["combined"][0]):])},
               "spy": {f"{k[0]}/{'after' if k[1] else 'pre'}": {x: v for x, v in s.items() if x not in ("dates", "equity")}
                       for k, s in spy.items()},
               "spy_block": {"pre": S.block(sp["dates"], np.array(sp["equity"]), np.array(sp["equity"])),
                             "after": S.block(sa["dates"], np.array(sa["equity"]), np.array(sa["equity"]))},
               "configs": {}, "mc": {}, "robust": {}, "every": {}}
    results["spy_tax_per_year_pct"] = sa["tax_per_year_pct"]
    curves = {"date": sp["dates"], "spy_pre": sp["equity"], "spy_after": sa["equity"]}
    for e in every:
        name = f"{e['variant']}/{e['universe']}"
        st = trade_stats(e["trades"], S.years({"start": PERIODS["combined"][0], "end": str(p.dates[-1])}))
        results["every"][name] = {**st, "counts": e["counts"]}
        with (OUT / f"every_signal_{e['variant']}_{e['universe']}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(e["trades"][0]))
            w.writeheader()
            w.writerows(e["trades"])
    for v, u in product(VARIANTS, UNIVERSES):
        name = f"{v}/{u}"
        cfg = {"periods": {}}
        for period in PERIODS:
            pre, post = runs[(f"{name}/{PRIMARY}/{period}", False)], runs[(f"{name}/{PRIMARY}/{period}", True)]
            spp, spa = spy[(period, False)], spy[(period, True)]
            cfg["periods"][period] = {
                "pre": {"cagr": pre["cagr_held"],
                        "max_drawdown": S.block(spp["dates"], np.array(pre["equity"]), np.array(spp["equity"]))["max_drawdown"]},
                "after_held": post["cagr_held"], "after_sold": post["cagr_sold"],
                "spy_pre": spp["cagr_held"], "spy_after_held": spa["cagr_held"], "spy_after_sold": spa["cagr_sold"],
                "trades": trade_stats(pre["trades"], pre["years"], with_paths=False)}
        pre, post = runs[(f"{name}/{PRIMARY}/combined", False)], runs[(f"{name}/{PRIMARY}/combined", True)]
        eq_pre, eq_post = np.array(pre["equity"]), np.array(post["equity"])
        cfg["pre"] = S.block(sp["dates"], eq_pre, np.array(sp["equity"]))
        cfg["after"] = S.block(sa["dates"], eq_post, np.array(sa["equity"]))
        cfg["tax"] = S.tax_stats(post)
        cfg["values"] = {"pre": pre["values"], "after": post["values"]}
        cfg["cagr"] = {"pre": pre["cagr_held"], "after_held": post["cagr_held"], "after_sold": post["cagr_sold"]}
        cfg["without_top5"] = pre["cagr_without_top5"]
        cfg["trades"] = trade_stats(pre["trades"], pre["years"])
        cfg["invested"] = 100 * float(np.mean(pre["invested"]))
        cfg["time_in_market"] = 100 * float(np.mean(np.array(pre["held"]) > 0))
        cfg["avg_positions"] = float(np.mean(pre["held"]))
        cfg["counts"] = pre["counts"]
        lev = cfg["pre"]["vol"] / spy_vol
        mpre, mpost = S.spy_account(*PERIODS["combined"], lev, False), S.spy_account(*PERIODS["combined"], lev, True)
        cfg["leverage_check"] = {"leverage": lev, "pre": mpre["cagr_held"], "after_held": mpost["cagr_held"],
                                 "after_sold": mpost["cagr_sold"], "interest_pct": 100 * mpre["interest_paid"] / CAPITAL,
                                 "max_drawdown": S.block(sp["dates"], np.array(mpre["equity"]),
                                                         np.array(sp["equity"]))["max_drawdown"]}
        cfg["ret63"] = {"pre": runs[(f"{name}/ret63/combined", False)]["cagr_held"],
                        "after_held": runs[(f"{name}/ret63/combined", True)]["cagr_held"]}
        results["configs"][name] = cfg
        results["robust"][name] = {label: {"pre": runs[(f"{name}/{label}", False)]["cagr_held"],
                                           "after_held": runs[(f"{name}/{label}", True)]["cagr_held"]}
                                   for label in neighbours(VARIANTS[v])}
        for taxed in (False, True):
            sub = [r for r in mc if r["variant"] == v and r["universe"] == u and r["taxed"] == taxed]
            c = np.array([r["cagr_held"] for r in sub])
            bench_c = sa["cagr_held"] if taxed else sp["cagr_held"]
            ranked = (post if taxed else pre)["cagr_held"]
            results["mc"][f"{name}/{'after' if taxed else 'pre'}"] = {
                "p5": S.pct(c, 5), "p50": S.pct(c, 50), "p95": S.pct(c, 95),
                "sold_p50": S.pct([r["cagr_sold"] for r in sub], 50),
                "dd_p50": S.pct([r["max_drawdown"] for r in sub], 50),
                "expectancy_p50": S.pct([r["expectancy_r"] for r in sub], 50),
                "beat_spy": 100 * float((c > bench_c).mean()), "ranked_percentile": 100 * float((c < ranked).mean())}
        curves[f"{v}_{u}_pre"] = pre["equity"]
        curves[f"{v}_{u}_after"] = post["equity"]
        with (OUT / f"trades_{v}_{u}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(pre["trades"][0]))
            w.writeheader()
            w.writerows(pre["trades"])
    with (OUT / "curves_combined.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = list(curves)
        w.writerow(cols)
        for i in range(len(curves["date"])):
            w.writerow([curves["date"][i], *(round(float(curves[c][i]), 2) for c in cols[1:])])
    with (OUT / "montecarlo.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(mc[0]))
        w.writeheader()
        w.writerows(mc)
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    (OUT / "report.md").write_text(report(results), encoding="utf-8")
    print(f"done in {time.time() - t0:.0f} s")


def report(res: dict) -> str:
    import ma_pullback_report
    return ma_pullback_report.report(res)


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
