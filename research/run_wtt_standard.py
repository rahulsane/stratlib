"""WTT and the QuantifiedStrategies-style version under the reporting standard of 2026-10-02.
Findings: output/wtt_std/report.md.

Strategies (rules unchanged; taxes are an accounting layer and never change a trade):
  wtt  Weekend Trend Trader as written: 20 positions of 5%, the 40% stop that tightens to 10% and never moves down.
  qs   the QuantifiedStrategies-style reconstruction: 10 positions of 10%, a 40% stop that never tightens.
Universes: point-in-time Russell 3000 and S&P 500 + MidCap 400 members at the signal week, as-traded close of $1.
Ranking (chosen before the run, the rule WTT lacks): 12-month momentum skipping the latest month, highest first.
Random selection (SEEDS runs) is the baseline; the 3-month return is shown as an alternative.
Costs: slippage 0.10% a side, 0.25% under $20. Dividends are included (FMP adjusted dividends, by ex-date).
Taxes (the standard): top federal rates (37% short-term, 20% long-term, plus 3.8%), no state tax; the year's tax is
paid from the account at the first session of the next year, cutting positions pro rata when cash is short;
lots first in, first out; wash sales; loss carryforwards; qualified-dividend test (tax_accounting.py).
Benchmarks: SPY with dividends, taxed the same way, held and sold at the end; SPY on margin at the strategy's
volatility (rebalanced monthly; margin at the 3-month T-bill rate plus 5.5%, at least 8%; idle cash earns nothing).
Robustness (primary ranking): doubled costs, entries a week late, each parameter one notch either way, the five
largest winners removed, 2016-2021 and 2022 onward run separately.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_standard.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_standard.py --report
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

import numpy as np

import benchmarks
import panel as P
import run_wtt as W
import run_wtt_qs as Q
from stratlib.app import open_context
from engine import OUTPUT, PERIODS, PERIOD_TITLES, simulate
from strategies.weekend_trend import WeekendTrend
from tax_accounting import END, NO_TAX, Accountant, TaxLedger, TaxRates

OUT = OUTPUT / "wtt_std"
DIV_PATH = P.CACHE / f"wtt_dividends_{W.THROUGH}.json"
SEEDS = 200
WORKERS = 10
RATES = TaxRates()
MARGIN_SPREAD, MARGIN_FLOOR = 5.5, 8.0
PRIMARY = "mom12_1"
CAPITAL = W.RULES.capital
STRATEGIES = {
    "wtt": ({"stop_rule": "ratchet"}, W.RULES, "WTT as written"),
    "qs": (Q.QS_PARAMS, Q.CONFIGS["qs_costs"][1], "QuantifiedStrategies-style"),
}
UNIVERSES = Q.UNIVERSES
RANKS = {"mom12_1": "12-month momentum skipping the latest month", "ret63": "3-month return",
         "random": "random (median of the runs)"}
NEIGHBOURS = {   # name -> parameter changes (the stop pair moves together for qs, whose stop never tightens)
    "breakout 15 weeks": {"high_weeks": 15}, "breakout 25 weeks": {"high_weeks": 25},
    "rate of change 25%": {"roc_pct": 25.0}, "rate of change 35%": {"roc_pct": 35.0},
    "index average 8 weeks": {"index_weeks": 8}, "index average 12 weeks": {"index_weeks": 12},
    "wide stop 30%": {"stop_up_pct": 30.0}, "wide stop 50%": {"stop_up_pct": 50.0},
    "tight stop 5%": {"stop_down_pct": 5.0}, "tight stop 15%": {"stop_down_pct": 15.0},
}
f, table, pct = W.f, W.table, W.pct


# ----------------------------------------------------------------------
# Data


def dividend_events(p) -> dict[int, list]:
    """{session: [(column, dividend per share), ...]} by ex-date (the first session on or after it), cached."""
    if not DIV_PATH.exists():
        ctx = open_context()
        events: dict[int, list] = {}
        try:
            for j, (s, kind) in enumerate(zip(p.symbols.tolist(), p.kind.tolist())):
                if kind != "stock":
                    continue
                doc = ctx.store.document("research:nash:div:" + s) or {}
                for ex, amount in (doc.get("rows") or {}).items():
                    t = int(np.searchsorted(p.dates, ex))
                    if amount and amount > 0 and 0 < t < len(p.dates):
                        events.setdefault(t, []).append((j, float(amount)))
        finally:
            ctx.close()
        DIV_PATH.write_text(json.dumps({str(k): v for k, v in events.items()}), encoding="utf-8")
    return {int(k): [tuple(x) for x in v] for k, v in json.loads(DIV_PATH.read_text(encoding="utf-8")).items()}


_W: dict = {}


def _init() -> None:
    Q._init()
    _W.update(Q._W, divs=dividend_events(Q._W["panel"]))
    p, b = _W["panel"], _W["bench"]
    rates = np.array([b["tbill3m"].get(str(d), np.nan) for d in p.dates], dtype=float)
    _W["tbill"] = np.nan_to_num(_ffill(rates))


def _ffill(a: np.ndarray) -> np.ndarray:
    out = a.copy()
    for k in range(1, len(out)):
        if not np.isfinite(out[k]):
            out[k] = out[k - 1]
    return out


# ----------------------------------------------------------------------
# Runs


def run_strategy(strategy: str, universe: str, rank: str, seed: int, period: str, taxed: bool,
                 extra: dict | None = None, rules_change: dict | None = None) -> dict:
    p = _W["panel"]
    params, rules, _ = STRATEGIES[strategy]
    params = {**params, "universe": universe, "rank": rank, "seed": seed, **(extra or {})}
    if strategy == "qs" and extra and "stop_up_pct" in extra:
        params["stop_down_pct"] = extra["stop_up_pct"]
    rules = replace(rules, **(rules_change or {}))
    s = WeekendTrend()
    s.setup(p, params)
    start, end = PERIODS[period]
    acc = Accountant(p, RATES if taxed else NO_TAX, _W["divs"])
    return simulate(p, s, start, end, rules, accountant=acc)


def terminal(run: dict) -> dict:
    """Ending values: as marked (taxes paid through the last full year), held (the last year's tax on realized
    gains paid too) and sold (and the tax on liquidating everything)."""
    eq = float(run["equity"][-1])
    return {"marked": eq, "held": eq - run["tax"]["tax_held"], "sold": eq - run["tax"]["tax_sold"]}


def years(run: dict) -> float:
    return (date.fromisoformat(run["end"]) - date.fromisoformat(run["start"])).days / 365.25


def cagr_of(value: float, yrs: float) -> float:
    return 100 * ((value / CAPITAL) ** (1 / yrs) - 1) if value > 0 else -100.0


def mc_task(job: tuple) -> dict:
    strategy, universe, seed, taxed = job
    run = run_strategy(strategy, universe, "random", seed, "combined", taxed)
    y, v = years(run), terminal(run)
    eq = np.concatenate([[CAPITAL], run["equity"]])
    return {"strategy": strategy, "universe": universe, "seed": seed, "taxed": taxed,
            "cagr_held": cagr_of(v["held"], y), "cagr_sold": cagr_of(v["sold"], y),
            "max_drawdown": 100 * float((1 - eq / np.maximum.accumulate(eq)).max())}


def fixed_task(job: tuple) -> dict:
    """One deterministic run: (key, strategy, universe, rank, period, taxed, extra params, rule changes)."""
    key, strategy, universe, rank, period, taxed, extra, rules_change = job
    run = run_strategy(strategy, universe, rank, 0, period, taxed, extra, rules_change)
    y, v = years(run), terminal(run)
    trades = run["trades"]
    pnl = sorted((t.pnl for t in trades), reverse=True)
    out = {"key": key, "strategy": strategy, "universe": universe, "rank": rank, "period": period, "taxed": taxed,
           "start": run["start"], "end": run["end"], "years": y, "values": v,
           "cagr_held": cagr_of(v["held"], y), "cagr_sold": cagr_of(v["sold"], y),
           "cagr_without_top5": cagr_of(v["marked"] - sum(pnl[:5]), y),
           "dates": [str(d) for d in run["dates"]], "equity": run["equity"].tolist(),
           "invested": (run["invested"] / run["equity"]).tolist(), "taxes_paid": run["taxes_paid"],
           "tax": {k: v for k, v in run["tax"].items()}, "counts": run["counts"],
           "trades": [{"ticker": t.ticker, "entry_date": t.entry_date, "exit_date": t.exit_date,
                       "return_pct": t.return_pct, "pnl": t.pnl, "holding_days": t.holding_days,
                       "exit_reason": t.exit_reason, "position_value": t.position_value} for t in trades]}
    return out


# ----------------------------------------------------------------------
# SPY, held or on margin, under the same tax rules


def margin_rate(tbill: float) -> float:
    return max(tbill + MARGIN_SPREAD, MARGIN_FLOOR)


def spy_account(start: str, end: str | None, leverage: float, taxed: bool) -> dict:
    """SPY bought at the first close. leverage 1: dividends reinvested at the ex-date close, the year's tax paid by
    selling SPY. Otherwise the account is rebalanced to `leverage` x equity at each month's first close, borrowing
    at margin_rate() (cash below zero) or holding idle cash; dividends and taxes go through cash."""
    p, b = _W["panel"], _W["bench"]
    j = p.index["SPY"]
    t0 = p.session_index(start)
    t1 = len(p.dates) - 1 if end is None else int(np.searchsorted(p.dates, end, side="right")) - 1
    price = p.close_ff[:, j]
    divs = b["spy_dividends"]
    days = [date.fromisoformat(str(d)) for d in p.dates]
    ledger = TaxLedger(RATES if taxed else NO_TAX)
    units = leverage * CAPITAL / price[t0]
    ledger.buy("SPY", days[t0], units, units * price[t0])
    cash = CAPITAL - units * price[t0]
    equity = [CAPITAL]
    interest_paid = taxes = 0.0
    payments: list[float] = []

    def trade_to(target_units: float, t: int):
        nonlocal units, cash
        diff = target_units - units
        if diff > 1e-9:
            ledger.buy("SPY", days[t], diff, diff * price[t])
        elif diff < -1e-9:
            ledger.sell("SPY", days[t], -diff, -diff * price[t])
        cash -= diff * price[t]
        units = target_units

    for t in range(t0 + 1, t1 + 1):
        if cash < 0:
            charge = -cash * margin_rate(_W["tbill"][t - 1]) / 100 * (days[t] - days[t - 1]).days / 365
            cash -= charge
            ledger.interest(days[t], charge)
            interest_paid += charge
        div = divs.get(str(p.dates[t]), 0.0)
        if div:
            cash += ledger.dividend("SPY", days[t], div)
        if days[t].year != days[t - 1].year:
            due = ledger.settle(days[t].year - 1, days[t])
            if due > 0:
                payments.append(100 * due / equity[-1])
            cash -= due
            taxes += due
        new_month = days[t].month != days[t - 1].month
        if leverage == 1.0:
            if abs(cash) > 1e-9:          # reinvest dividends, or sell to pay the tax
                trade_to(units + cash / price[t], t)
        elif new_month:
            trade_to(leverage * (units * price[t] + cash) / price[t], t)
        equity.append(units * price[t] + cash)
    held = ledger.finish(days[t1].year, days[t1])["tax_held"]
    ledger.sell("SPY", days[t1], units, units * price[t1], END)
    sold = ledger.finish(days[t1].year, days[t1])["tax_sold"]
    eq = np.array(equity)
    yrs = (days[t1] - days[t0]).days / 365.25
    return {"dates": [str(d) for d in p.dates[t0:t1 + 1]], "equity": eq.tolist(), "years": yrs,
            "values": {"marked": float(eq[-1]), "held": float(eq[-1] - held), "sold": float(eq[-1] - sold)},
            "cagr_held": cagr_of(float(eq[-1] - held), yrs), "cagr_sold": cagr_of(float(eq[-1] - sold), yrs),
            "interest_paid": interest_paid, "taxes": taxes, "taxes_total": taxes + held,
            "tax_per_year_pct": float(np.mean(payments)) if payments else 0.0,
            "stats": dict(ledger.stats), "leverage": leverage}


# ----------------------------------------------------------------------
# The volatility block


def block(dates: list[str], eq: np.ndarray, bench: np.ndarray) -> dict:
    """Risk and SPY-relative measures for a daily equity curve (first value = starting capital) against a
    benchmark curve on the same sessions."""
    r = eq[1:] / eq[:-1] - 1
    dd = 1 - eq / np.maximum.accumulate(eq)
    under = longest = 0
    for x in dd:
        under = under + 1 if x > 1e-12 else 0
        longest = max(longest, under)
    month_ends = [i for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]] + [len(dates) - 1]
    yearly = {}
    prev = eq[0]
    for i in range(len(dates)):
        if i == len(dates) - 1 or dates[i][:4] != dates[i + 1][:4]:
            yearly[dates[i][:4]] = 100 * (eq[i] / prev - 1)
            prev = eq[i]
    roll = eq[252:] / eq[:-252] - 1
    broll = bench[252:] / bench[:-252] - 1

    def ahead(months: int) -> float | None:
        me = np.array(month_ends)
        pairs = [(a, z) for a, z in zip(me[:-months], me[months:])]
        if not pairs:
            return None
        return 100 * float(np.mean([eq[z] / eq[a] > bench[z] / bench[a] for a, z in pairs]))

    rel = eq / bench
    rel_dd = 1 - rel / np.maximum.accumulate(rel)
    behind = run_ = 0
    for x in rel_dd:
        run_ = run_ + 1 if x > 1e-12 else 0
        behind = max(behind, run_)
    return {"vol": 100 * float(r.std(ddof=1) * math.sqrt(252)), "max_drawdown": 100 * float(dd.max()),
            "worst_year": min(yearly.values()), "worst_year_label": min(yearly, key=yearly.get),
            "worst_12m": 100 * float(roll.min()) if len(roll) else None,
            "longest_underwater_months": longest / 21, "ahead_3y": ahead(36), "ahead_5y": ahead(60),
            "longest_behind_months": behind / 21,
            "worst_12m_shortfall": 100 * float((roll - broll).min()) if len(roll) else None,
            "yearly": yearly}


def tax_stats(run: dict) -> dict:
    eq = np.concatenate([[CAPITAL], run["equity"]])
    dates = run["dates"]
    shares = []
    for day, amount in run["taxes_paid"]:
        i = dates.index(day)
        shares.append(100 * amount / eq[i])          # eq is offset by one: eq[i] is the previous session's value
    st = run["tax"]["stats"]
    gains = st.get("realized_st_gains", 0.0) + st.get("realized_lt_gains", 0.0)
    carry = run["tax"]["carry_held"]
    return {"tax_per_year_pct": float(np.mean(shares)) if shares else 0.0, "tax_years": len(shares),
            "st_share_of_gains": 100 * st.get("realized_st_gains", 0.0) / gains if gains else None,
            "wash_sales": int(st.get("wash_sales", 0)), "wash_disallowed": st.get("wash_disallowed", 0.0),
            "carry_st": carry[0], "carry_lt": carry[1], "dividends": st.get("dividends", 0.0),
            "taxes_total": sum(a for _, a in run["taxes_paid"]) + run["tax"]["tax_held"]}


# ----------------------------------------------------------------------
# Main


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    _init()
    print(f"panel {_W['panel'].close.shape}, dividend sessions {len(_W['divs'])}", flush=True)
    fixed_jobs = []
    for s in STRATEGIES:
        for u in UNIVERSES:
            for taxed in (False, True):
                for period in PERIODS:
                    fixed_jobs.append((f"{s}/{u}/{PRIMARY}/{period}", s, u, PRIMARY, period, taxed, None, None))
                fixed_jobs.append((f"{s}/{u}/ret63/combined", s, u, "ret63", "combined", taxed, None, None))
                fixed_jobs.append((f"{s}/{u}/double costs", s, u, PRIMARY, "combined", taxed, None,
                                   {"slippage_pct": 0.20, "slippage_low_price_pct": 0.50}))
                fixed_jobs.append((f"{s}/{u}/entries a week late", s, u, PRIMARY, "combined", taxed,
                                   {"entry_delay_weeks": 1}, None))
                for name, change in NEIGHBOURS.items():
                    if s == "qs" and "stop_down_pct" in change:
                        continue
                    fixed_jobs.append((f"{s}/{u}/{name}", s, u, PRIMARY, "combined", taxed, change, None))
    mc_jobs = [(s, u, seed, taxed) for s in STRATEGIES for u in UNIVERSES for seed in range(SEEDS)
               for taxed in (False, True)]
    with ProcessPoolExecutor(WORKERS, initializer=_init) as pool:
        fixed = list(pool.map(fixed_task, fixed_jobs, chunksize=2))
        print(f"fixed runs: {len(fixed)}, {time.time() - t0:.0f} s", flush=True)
        mc = list(pool.map(mc_task, mc_jobs, chunksize=8))
    print(f"random runs: {len(mc)}, {time.time() - t0:.0f} s", flush=True)

    runs = {(r["key"], r["taxed"]): r for r in fixed}
    spy = {}
    for period, (start, end) in PERIODS.items():
        for taxed in (False, True):
            spy[(period, taxed)] = spy_account(start, end, 1.0, taxed)
    results = {"data_through": str(_W["panel"].dates[-1]), "seeds": SEEDS, "rates": asdict(RATES),
               "margin": {"spread": MARGIN_SPREAD, "floor": MARGIN_FLOOR, "today": margin_rate(float(_W["tbill"][-1]))},
               "spy": {f"{k[0]}/{'after' if k[1] else 'pre'}": {x: v for x, v in s.items() if x not in ("dates", "equity")}
                       for k, s in spy.items()},
               "configs": {}, "mc": {}, "robust": {}}
    curves = {"date": spy[("combined", False)]["dates"],
              "spy_pre": spy[("combined", False)]["equity"], "spy_after": spy[("combined", True)]["equity"]}
    for s in STRATEGIES:
        for u in UNIVERSES:
            name = f"{s}/{u}"
            cfg = {"periods": {}}
            for period in PERIODS:
                pre, post = runs[(f"{name}/{PRIMARY}/{period}", False)], runs[(f"{name}/{PRIMARY}/{period}", True)]
                sp, sa = spy[(period, False)], spy[(period, True)]
                cfg["periods"][period] = {
                    "pre": {"cagr": pre["cagr_held"], "max_drawdown": block(
                        sp["dates"], np.array(pre["equity"]), np.array(sp["equity"]))["max_drawdown"]},
                    "after_held": post["cagr_held"], "after_sold": post["cagr_sold"],
                    "spy_pre": sp["cagr_held"], "spy_after_held": sa["cagr_held"], "spy_after_sold": sa["cagr_sold"]}
            pre, post = runs[(f"{name}/{PRIMARY}/combined", False)], runs[(f"{name}/{PRIMARY}/combined", True)]
            sp, sa = spy[("combined", False)], spy[("combined", True)]
            # The engine's curve starts at the first session's close, as the SPY account's does.
            eq_pre, eq_post = np.array(pre["equity"]), np.array(post["equity"])
            cfg["pre"] = block(sp["dates"], eq_pre, np.array(sp["equity"]))
            cfg["after"] = block(sa["dates"], eq_post, np.array(sa["equity"]))
            cfg["tax"] = tax_stats(post)
            cfg["values"] = {"pre": pre["values"], "after": post["values"]}
            cfg["cagr"] = {"pre": pre["cagr_held"], "after_held": post["cagr_held"], "after_sold": post["cagr_sold"]}
            cfg["without_top5"] = pre["cagr_without_top5"]
            cfg["trades"] = trade_summary(pre["trades"])
            cfg["invested"] = 100 * float(np.mean(pre["invested"]))
            lev = cfg["pre"]["vol"] / block(sp["dates"], np.array(sp["equity"]), np.array(sp["equity"]))["vol"]
            mpre, mpost = spy_account(*PERIODS["combined"], lev, False), spy_account(*PERIODS["combined"], lev, True)
            cfg["leverage_check"] = {"leverage": lev, "pre": mpre["cagr_held"], "after_held": mpost["cagr_held"],
                                     "after_sold": mpost["cagr_sold"], "interest_pct": 100 * mpre["interest_paid"]
                                     / CAPITAL, "max_drawdown": block(sp["dates"], np.array(mpre["equity"]),
                                                                      np.array(sp["equity"]))["max_drawdown"]}
            cfg["ret63"] = {"pre": runs[(f"{name}/ret63/combined", False)]["cagr_held"],
                            "after_held": runs[(f"{name}/ret63/combined", True)]["cagr_held"]}
            results["configs"][name] = cfg
            robust = {}
            for label in ["double costs", "entries a week late", *NEIGHBOURS]:
                key = f"{name}/{label}"
                if (key, False) in runs:
                    robust[label] = {"pre": runs[(key, False)]["cagr_held"], "after_held": runs[(key, True)]["cagr_held"]}
            results["robust"][name] = robust
            for taxed in (False, True):
                sub = [r for r in mc if r["strategy"] == s and r["universe"] == u and r["taxed"] == taxed]
                key = f"{name}/{'after' if taxed else 'pre'}"
                c = np.array([r["cagr_held"] for r in sub])
                bench_c = sa["cagr_held"] if taxed else sp["cagr_held"]
                results["mc"][key] = {"p5": pct(c, 5), "p50": pct(c, 50), "p95": pct(c, 95),
                                      "sold_p50": pct([r["cagr_sold"] for r in sub], 50),
                                      "dd_p50": pct([r["max_drawdown"] for r in sub], 50),
                                      "beat_spy": 100 * float((c > bench_c).mean()),
                                      "ranked_percentile": 100 * float((c < (post if taxed else pre)["cagr_held"]).mean())}
            curves[f"{s}_{u}_pre"] = pre["equity"]
            curves[f"{s}_{u}_after"] = post["equity"]
            with (OUT / f"trades_{s}_{u}.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(post["trades"][0]))
                w.writeheader()
                w.writerows(post["trades"])
    results["spy_block"] = {"pre": block(spy[("combined", False)]["dates"], np.array(spy[("combined", False)]["equity"]),
                                         np.array(spy[("combined", False)]["equity"])),
                            "after": block(spy[("combined", True)]["dates"], np.array(spy[("combined", True)]["equity"]),
                                           np.array(spy[("combined", True)]["equity"]))}
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


def trade_summary(trades: list[dict]) -> dict:
    ret = np.array([t["return_pct"] for t in trades])
    weeks = np.array([t["holding_days"] / 7 for t in trades])
    win = ret > 0
    return {"trades": len(trades), "win_rate": 100 * float(win.mean()), "avg_win": float(ret[win].mean()),
            "avg_loss": float(ret[~win].mean()), "weeks": float(np.median(weeks)),
            "weeks_win": float(np.mean(weeks[win])), "weeks_loss": float(np.mean(weeks[~win]))}


def report(res: dict) -> str:
    import wtt_std_report
    return wtt_std_report.report(res)


def complete(res: dict) -> dict:
    """Fill in SPY figures added to the report after a run (cheap: the SPY account only)."""
    if "spy_tax_per_year_pct" not in res or "max" not in res["margin"]:
        _init()
        a = spy_account(*PERIODS["combined"], 1.0, True)
        res["spy"]["combined/after"].update(taxes_total=a["taxes_total"], tax_per_year_pct=a["tax_per_year_pct"])
        res["spy_tax_per_year_pct"] = a["tax_per_year_pct"]
        study = _W["panel"].session_index(PERIODS["combined"][0])
        res["margin"]["max"] = max(margin_rate(float(x)) for x in _W["tbill"][study:])
    return res


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = complete(json.loads((OUT / "results.json").read_text(encoding="utf-8")))
        (OUT / "results.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
