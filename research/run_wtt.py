"""Nick Radge's Weekend Trend Trader with two stop rules (strategies/weekend_trend.py). Findings: output/wtt/report.md.

Variants:
  ratchet    as described in the book: the stop never moves down, so after a week with the index below its
             10-week average it stays at the 10% level until 40% below a new high climbs past it.
  recompute  the stop is recomputed each week from the highest close, so it loosens back to 40% when the
             index recovers (how some coded versions behave).

Universe: common stocks of US companies (FMP's country, or an ADR flag, for delisted stocks; the Russell 3000
holds US companies only), within the ground rules' liquidity floor ($5 as traded and $20M of 20-day average
dollar volume at the signal week's close). Size: 5% of equity per position, at most 20 (the book), instead of
the ground rules' risk sizing. Costs, periods, cash and the SPY benchmark follow the ground rules.

The book gives no rule for choosing among more signals than free slots. The headline is SEEDS random
selections per variant and period, the method Radge suggests for removing selection bias. Fixed rankings
for comparison: the ground rules' 63-session return and the 20-week rate of change, highest first, plus the
63-session ranking with foreign companies included.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt.py --report   (from results.json)
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date

import numpy as np

import benchmarks
import panel as P
from stratlib.app import open_context
from engine import OUTPUT, PERIODS, PERIOD_TITLES, Rules, metrics, run_test, simulate, spy_series, yearly
from strategies.weekend_trend import WeekendTrend

THROUGH = "2026-09-30"
OUT = OUTPUT / "wtt"
RULES = Rules(max_positions=20, max_position_pct=5.0)
SEEDS = 200
WORKERS = 14
RULE_NAMES = {"ratchet": "Stop as described (never moves down)", "recompute": "Recomputed stop (loosens again)"}
FIXED = {  # test name -> (stop rule, universe, ranking)
    "wtt_ratchet_ret63": ("ratchet", "us", "ret63"),
    "wtt_recompute_ret63": ("recompute", "us", "ret63"),
    "wtt_ratchet_roc": ("ratchet", "us", "roc"),
    "wtt_recompute_roc": ("recompute", "us", "roc"),
    "wtt_ratchet_ret63_all_countries": ("ratchet", "all", "ret63"),
    "wtt_recompute_ret63_all_countries": ("recompute", "all", "ret63"),
}
RANK_NAMES = {"random": "random (median of the runs)", "ret63": "63-session return, highest first",
              "roc": "20-week rate of change, highest first"}
MC_KEYS = ("trades", "win_rate", "avg_win_pct", "avg_loss_pct", "profit_factor", "total_return", "cagr",
           "max_drawdown", "sharpe", "exposure_time_pct", "exposure_avg_invested_pct")


def foreign_companies(panel) -> frozenset:
    """Stocks of non-US companies: FMP's country for current stocks, the saved profile (country or ADR flag) for
    delisted ones. A blank country counts as US."""
    ctx = open_context()
    try:
        con = sqlite3.connect(ctx.store.db_path)
        country = {s: c or "" for s, c in con.execute("select symbol, country from symbols")}
        con.close()
        out = set()
        for s, kind, until in zip(panel.symbols.tolist(), panel.kind.tolist(), panel.until.tolist()):
            if kind != "stock":
                continue
            if until:
                profile = (ctx.store.document("backtest:approx:profile:" + s) or {}).get("profile") or {}
                c, adr = profile.get("country") or "", bool(profile.get("isAdr"))
            else:
                c, adr = country.get(s, ""), False
            if adr or (c and c != "US"):
                out.add(s)
        return frozenset(out)
    finally:
        ctx.close()


# ----------------------------------------------------------------------
# Runs

_W: dict = {}


def _init() -> None:
    p, b = P.load(through=THROUGH), benchmarks.load()
    WeekendTrend.foreign = foreign_companies(p)
    _W.update(panel=p, bench=b, spy={k: spy_series(p, b["spy_dividends"], s, e or str(p.dates[-1]))
                                     for k, (s, e) in PERIODS.items()})


def params_for(rule: str, universe: str, rank: str, seed: int = 0) -> dict:
    return {"stop_rule": rule, "universe": universe, "rank": rank, "seed": seed}


def mc_task(task: tuple) -> dict:
    rule, period, seed = task
    p, b = _W["panel"], _W["bench"]
    start, end = PERIODS[period]
    s = WeekendTrend()
    s.setup(p, params_for(rule, "us", "random", seed))
    run = simulate(p, s, start, end, RULES)
    m = metrics(run, _W["spy"][period], b["tbill3m"])
    row = {"rule": rule, "period": period, "seed": seed, **{k: float(m[k]) for k in MC_KEYS},
           "signals": m["counts"]["signals"], "no_slot": m["counts"]["skipped_no_slot"], "yearly": m["yearly"]}
    if period == "combined":
        row["equity"] = run["equity"].astype(np.float32)
    return row


def curve_stats(equity: np.ndarray, dates: np.ndarray, spy: np.ndarray) -> dict:
    """equity: the run's closes; dates and spy start one session earlier (the starting capital's date)."""
    eq = np.concatenate([[RULES.capital], equity])
    r = eq[1:] / eq[:-1] - 1
    sr = spy[1:] / spy[:-1] - 1
    month_ends = [0] + [i for i in range(1, len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]] + [len(dates) - 1]
    me = eq[month_ends]
    mr = me[1:] / me[:-1] - 1
    dd = eq / np.maximum.accumulate(eq) - 1
    longest = run_ = 0
    for x in dd:
        run_ = run_ + 1 if x < 0 else 0
        longest = max(longest, run_)
    return {"vol": 100 * float(r.std(ddof=1) * math.sqrt(252)), "worst_month": 100 * float(mr.min()),
            "longest_underwater_months": longest / 21, "beta": float(np.cov(r, sr)[0, 1] / sr.var(ddof=1)),
            "corr_spy": float(np.corrcoef(r, sr)[0, 1])}


def trade_stats(run: dict, strategy: WeekendTrend) -> dict:
    trades = run["trades"]
    pct = np.array([t.return_pct for t in trades])
    pnl = np.array([t.pnl for t in trades])
    weeks = np.array([t.holding_days / 7 for t in trades])
    win = pnl > 0
    total = pnl.sum()
    top = np.sort(pnl)[::-1][:max(1, math.ceil(len(pnl) / 10))].sum()
    exits = Counter(t.exit_reason for t in trades)
    events = Counter(e for _, _, e in strategy.events)
    years = (date.fromisoformat(run["end"]) - date.fromisoformat(run["start"])).days / 365.25
    return {"trades": len(trades), "per_year": len(trades) / years, "win_rate": 100 * float(win.mean()),
            "avg_win_pct": float(pct[win].mean()), "avg_loss_pct": float(pct[~win].mean()),
            "payoff": float(pct[win].mean() / -pct[~win].mean()), "expectancy_pct": float(pct.mean()),
            "median_pct": float(np.median(pct)), "profit_factor": float(pnl[win].sum() / -pnl[~win].sum()),
            "hold_win_weeks": float(weeks[win].mean()), "hold_loss_weeks": float(weeks[~win].mean()),
            "losses_over_40": int((pct < -40).sum()), "top10_share": 100 * float(top / total) if total > 0 else None,
            "exit_40": exits.get("weekly close under the 40% stop", 0),
            "exit_10": exits.get("weekly close under the 10% stop", 0),
            "exit_delisted": exits.get("delisted", 0), "exit_end": exits.get("end of test", 0),
            "tightened": events.get("tightened", 0), "loosened": events.get("loosened", 0),
            "avg_held": float(np.mean(run["held"])), "signals": run["counts"]["signals"],
            "entries": run["counts"]["entries"], "no_slot": run["counts"]["skipped_no_slot"],
            "no_cash": run["counts"]["skipped_no_cash"]}


def detail(p, b, spy: dict, params: dict, rules: Rules = RULES) -> dict:
    """Combined-period detail for one fixed configuration."""
    s = WeekendTrend()
    s.setup(p, params)
    start, end = PERIODS["combined"]
    run = simulate(p, s, start, end, rules)
    m = metrics(run, spy["combined"], b["tbill3m"])
    dates = run["dates"]
    spy_curve = spy["combined"]["total"]
    return {"metrics": {k: float(m[k]) for k in MC_KEYS}, "yearly": m["yearly"],
            "curve": curve_stats(run["equity"], np.concatenate([[dates[0]], dates]),
                                 np.concatenate([[spy_curve[0]], spy_curve])),
            "trades": trade_stats(run, s), "equity": run["equity"]}


def pct(values, q):
    return float(np.percentile(values, q))


def index_filter_stats(p) -> dict:
    """Weeks with SPY below its 10-week average, and the separate spells of them, per period."""
    s = WeekendTrend()
    s.setup(p, params_for("ratchet", "all", "ret63"))
    out = {}
    for period, (start, end) in PERIODS.items():
        rows = [t for t in np.flatnonzero(s.week_end) if start <= str(p.dates[t]) <= (end or "9999")]
        down = ~s.market_up[rows]
        out[period] = {"weeks": len(rows), "down_pct": 100 * float(down.mean()),
                       "spells": int(down[0] + np.sum(down[1:] & ~down[:-1]))}
    return out


def main() -> None:
    t_start = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    _init()
    p, b, spy = _W["panel"], _W["bench"], _W["spy"]
    print(f"panel {p.dates[0]}..{p.dates[-1]}, {len(WeekendTrend.foreign)} stocks of foreign companies left out")

    tasks = [(rule, period, seed) for rule in RULE_NAMES for period in PERIODS for seed in range(SEEDS)]
    with ProcessPoolExecutor(WORKERS, initializer=_init) as pool:
        rows = list(pool.map(mc_task, tasks, chunksize=8))
    print(f"Monte Carlo: {len(rows)} runs, {time.time() - t_start:.0f} s")

    with (OUT / "montecarlo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rule", "period", "seed", *MC_KEYS, "signals", "skipped_no_slot"])
        for r in rows:
            w.writerow([r["rule"], r["period"], r["seed"], *(round(r[k], 4) for k in MC_KEYS), r["signals"], r["no_slot"]])

    spy_stats = {}
    for period in PERIODS:
        s = spy[period]
        start, end = PERIODS[period]
        dates = p.dates[p.session_index(start):int(np.searchsorted(p.dates, end or str(p.dates[-1]), side="right"))]
        years = (date.fromisoformat(str(dates[-1])) - date.fromisoformat(str(dates[0]))).days / 365.25
        spy_stats[period] = {"cagr": 100 * (s["total"][-1] ** (1 / years) - 1),
                             "cagr_price": 100 * (s["price"][-1] ** (1 / years) - 1),
                             "max_drawdown": 100 * float((1 - s["total"] / np.maximum.accumulate(s["total"])).max()),
                             "start": str(dates[0]), "end": str(dates[-1])}
        if period == "combined":
            spy_yearly = yearly(s["total"], dates)

    mc = {}
    for rule in RULE_NAMES:
        for period in PERIODS:
            sub = [r for r in rows if r["rule"] == rule and r["period"] == period]
            c = np.array([r["cagr"] for r in sub])
            mc[f"{rule}/{period}"] = {
                **{f"{k}_p{q}": pct([r[k] for r in sub], q) for k in ("cagr", "max_drawdown", "sharpe")
                   for q in (5, 50, 95)},
                **{f"{k}_median": pct([r[k] for r in sub], 50) for k in MC_KEYS},
                "beat_spy_total": 100 * float((c > spy_stats[period]["cagr"]).mean()),
                "beat_spy_price": 100 * float((c > spy_stats[period]["cagr_price"]).mean()),
                "positive": 100 * float((c > 0).mean()),
                "yearly_median": {y: pct([r["yearly"][y] for r in sub], 50) for y in sub[0]["yearly"]},
                "yearly_p5": {y: pct([r["yearly"][y] for r in sub], 5) for y in sub[0]["yearly"]},
                "yearly_p95": {y: pct([r["yearly"][y] for r in sub], 95) for y in sub[0]["yearly"]},
            }
    paired = {}
    for period in PERIODS:
        a = {r["seed"]: r["cagr"] for r in rows if r["rule"] == "ratchet" and r["period"] == period}
        z = {r["seed"]: r["cagr"] for r in rows if r["rule"] == "recompute" and r["period"] == period}
        d = np.array([z[k] - a[k] for k in a])
        paired[period] = {"median_diff": pct(d, 50), "p5": pct(d, 5), "p95": pct(d, 95),
                          "recompute_better": 100 * float((d > 0).mean())}

    # Median-seed run per rule (combined CAGR), saved with the fixed rankings through the harness runner.
    median_seed = {}
    for rule in RULE_NAMES:
        sub = sorted((r for r in rows if r["rule"] == rule and r["period"] == "combined"), key=lambda r: r["cagr"])
        median_seed[rule] = sub[len(sub) // 2]["seed"]
    tests = {f"wtt_{rule}_random_median": (rule, "us", "random", median_seed[rule]) for rule in RULE_NAMES}
    tests.update({k: (*v, 0) for k, v in FIXED.items()})
    fixed = {}
    for name, (rule, universe, rank, seed) in tests.items():
        params = params_for(rule, universe, rank, seed)
        desc = (f"Weekend Trend Trader. {RULE_NAMES[rule]}. Universe: common stocks"
                f"{' of US companies' if universe == 'us' else ''} within the liquidity floor. Ranking: "
                f"{'random, seed ' + str(seed) + ' (the median of ' + str(SEEDS) + ' random runs on the combined period)' if rank == 'random' else RANK_NAMES[rank]}. "
                "5% of equity per position, at most 20. See output/wtt/report.md.")
        summary = run_test(p, name, WeekendTrend, params, b, rules=RULES, description=desc)
        fixed[name] = {"rule": rule, "universe": universe, "rank": rank, "seed": seed,
                       "periods": {k: {m: float(v[m]) for m in MC_KEYS} for k, v in summary["results"].items()},
                       **{k: v for k, v in detail(p, b, spy, params).items() if k != "equity"}}
        print(name, {k: round(v["cagr"], 1) for k, v in fixed[name]["periods"].items()})

    # Curves for the chart: pointwise percentiles of the random runs, the fixed rankings and SPY.
    start = PERIODS["combined"][0]
    dates = p.dates[p.session_index(start):]
    cols = {"spy_total": RULES.capital * spy["combined"]["total"], "spy_price": RULES.capital * spy["combined"]["price"]}
    for rule in RULE_NAMES:
        eq = np.array([r["equity"] for r in rows if r["rule"] == rule and r["period"] == "combined"], dtype=float)
        dd = 1 - eq / np.maximum.accumulate(eq, axis=1)
        for q in (5, 50, 95):
            cols[f"{rule}_p{q}"] = np.percentile(eq, q, axis=0)
            cols[f"{rule}_dd_p{q}"] = -100 * np.percentile(dd, 100 - q, axis=0)
        s = WeekendTrend()
        s.setup(p, params_for(rule, "us", "ret63"))
        cols[f"{rule}_ret63"] = simulate(p, s, start, None, RULES)["equity"]
    with (OUT / "curves_combined.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", *cols])
        for i, d in enumerate(dates):
            w.writerow([d, *(round(float(v[i]), 2) for v in cols.values())])

    results = {"data_through": str(p.dates[-1]), "seeds": SEEDS, "rules": RULES.__dict__,
               "foreign_left_out": len(WeekendTrend.foreign), "spy": spy_stats, "spy_yearly": spy_yearly,
               "index_filter": index_filter_stats(p), "mc": mc, "paired": paired,
               "median_seed": median_seed, "fixed": fixed, "panel_notes": {
                   k: v for k, v in p.notes.items() if k in ("securities", "stocks", "delisted_stocks")}}
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    (OUT / "report.md").write_text(report(results), encoding="utf-8")
    print(f"done in {time.time() - t_start:.0f} s")


# ----------------------------------------------------------------------
# Report


def f(x, digits=1, suffix="", sign=False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    if round(x, digits) == 0:
        x = 0.0   # no "-0.0"
    return f"{x:+,.{digits}f}{suffix}" if sign else f"{x:,.{digits}f}{suffix}"


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def report(res: dict) -> str:
    mc, spy, fx, paired = res["mc"], res["spy"], res["fixed"], res["paired"]
    n = res["seeds"]
    periods = ["combined", "in_sample", "out_of_sample"]
    title = {p: PERIOD_TITLES[p] for p in periods}
    out = ["# Nick Radge's Weekend Trend Trader, two stop rules", "",
           f"Data through {res['data_through']}. Rules and method below; the strategy is in "
           "`src/stratlib/sim/strategies/weekend_trend.py` and the runner in `research/run_wtt.py`.", "",
           "## Findings", "", findings(res), "", "![Growth of $100,000 and drawdowns](equity_drawdown.png)", ""]

    out += ["## Random selection: CAGR, drawdown and Sharpe", "",
            f"{n} runs per stop rule and period. Each run takes the signals in a different random order when there "
            "are more than free slots. Percentiles are across runs: 5th / median / 95th.", ""]
    rows = []
    for period in periods:
        for rule in RULE_NAMES:
            m = mc[f"{rule}/{period}"]
            rows.append([title[period], RULE_NAMES[rule],
                         f"{f(m['cagr_p5'])}% / **{f(m['cagr_p50'])}%** / {f(m['cagr_p95'])}%",
                         f"{f(m['max_drawdown_p5'], 0)}% / {f(m['max_drawdown_p50'], 0)}% / {f(m['max_drawdown_p95'], 0)}%",
                         f"{f(m['sharpe_p50'], 2)}", f"{f(m['beat_spy_total'], 0)}% / {f(m['beat_spy_price'], 0)}%"])
        s = spy[period]
        rows.append([title[period], "SPY buy-and-hold", f"**{f(s['cagr'])}%** with dividends, {f(s['cagr_price'])}% price",
                     f"{f(s['max_drawdown'], 0)}%", "", ""])
    out += [table(["Period", "Variant", "CAGR", "Max drawdown", "Sharpe (median)",
                   "Runs beating SPY: with dividends / price only"], rows), ""]
    out += ["- " + "; ".join(f"{title[p]}: {spy[p]['start']} to {spy[p]['end']}" for p in periods) + ".",
            "- Each period is a separate run from $100,000. Trade returns exclude dividends and idle cash earns "
            "nothing (ground rules), so the price-only SPY figure is the like-for-like one.", ""]

    out += ["## Ranking decides the result", "",
            "Combined period (2016–present). The fixed rankings are single deterministic runs; the random row is "
            "the median run.", ""]
    rows = []
    for rank, universe in (("random", "us"), ("ret63", "us"), ("roc", "us"), ("ret63", "all")):
        cells = [RANK_NAMES[rank] + ("" if universe == "us" else ", foreign companies included")]
        for rule in RULE_NAMES:
            if rank == "random":
                m = mc[f"{rule}/combined"]
                cells.append(f"{f(m['cagr_p50'])}% / {f(m['max_drawdown_p50'], 0)}%")
            else:
                name = f"wtt_{rule}_{rank}" + ("_all_countries" if universe == "all" else "")
                m = fx[name]["periods"]["combined"]
                cells.append(f"{f(m['cagr'])}% / {f(m['max_drawdown'], 0)}%")
        rows.append(cells)
    rows.append(["SPY buy-and-hold (with dividends / price only)",
                 f"{f(spy['combined']['cagr'])}% / {f(spy['combined']['cagr_price'])}%", ""])
    out += [table(["Ranking: CAGR / max drawdown", *RULE_NAMES.values()], rows), ""]
    rows = []
    for rank in ("ret63", "roc"):
        for period in ("in_sample", "out_of_sample"):
            rows.append([RANK_NAMES[rank], title[period]] +
                        [f"{f(fx[f'wtt_{rule}_{rank}']['periods'][period]['cagr'])}% / "
                         f"{f(fx[f'wtt_{rule}_{rank}']['periods'][period]['max_drawdown'], 0)}%" for rule in RULE_NAMES])
    out += ["Fixed rankings by period (CAGR / max drawdown):", "",
            table(["Ranking", "Period", *RULE_NAMES.values()], rows), ""]

    out += ["## The two stop rules compared", "",
            f"Paired by random seed (the same random order of signals, though the portfolios drift apart). "
            "CAGR of the recomputed stop minus the stop as described:", ""]
    out += [table(["Period", "Median difference", "5th–95th percentile", "Runs where the recomputed stop did better"],
                  [[title[p], f(paired[p]["median_diff"], 2, " pts", sign=True),
                    f"{f(paired[p]['p5'], 1, sign=True)} to {f(paired[p]['p95'], 1, sign=True)} pts",
                    f"{f(paired[p]['recompute_better'], 0)}%"] for p in periods]), ""]

    names = [f"wtt_{rule}_random_median" for rule in RULE_NAMES]
    out += ["Trade detail for the median random run of each rule (combined period; seed "
            + ", ".join(f"{res['median_seed'][r]}" for r in RULE_NAMES) + "):", ""]

    def trow(label, fn):
        return [label] + [fn(fx[nm]) for nm in names]

    t = lambda x: x["trades"]
    c = lambda x: x["curve"]
    m = lambda x: x["metrics"]
    rows = [
        trow("CAGR / max drawdown", lambda x: f"{f(m(x)['cagr'])}% / {f(m(x)['max_drawdown'], 0)}%"),
        trow("Sharpe / volatility", lambda x: f"{f(m(x)['sharpe'], 2)} / {f(c(x)['vol'], 0)}%"),
        trow("Worst month / longest time under a previous high",
             lambda x: f"{f(c(x)['worst_month'])}% / {f(c(x)['longest_underwater_months'], 0)} months"),
        trow("Beta / correlation to SPY (daily)", lambda x: f"{f(c(x)['beta'], 2)} / {f(c(x)['corr_spy'], 2)}"),
        trow("Average invested / positions held", lambda x: f"{f(m(x)['exposure_avg_invested_pct'], 0)}% / {f(t(x)['avg_held'], 1)}"),
        trow("Trades / per year", lambda x: f"{t(x)['trades']:,} / {f(t(x)['per_year'], 0)}"),
        trow("Win rate", lambda x: f"{f(t(x)['win_rate'])}%"),
        trow("Average win / average loss", lambda x: f"{f(t(x)['avg_win_pct'], 1, '%', True)} / {f(t(x)['avg_loss_pct'], 1, '%', True)}"),
        trow("Payoff ratio (average win / average loss)", lambda x: f(t(x)["payoff"], 2)),
        trow("Average / median trade", lambda x: f"{f(t(x)['expectancy_pct'], 1, '%', True)} / {f(t(x)['median_pct'], 1, '%', True)}"),
        trow("Profit factor", lambda x: f(t(x)["profit_factor"], 2)),
        trow("Average holding: winners / losers", lambda x: f"{f(t(x)['hold_win_weeks'], 0)} / {f(t(x)['hold_loss_weeks'], 0)} weeks"),
        trow("Losses worse than -40%", lambda x: f"{t(x)['losses_over_40']}"),
        trow("Top 10% of trades, share of profit", lambda x: f(t(x)["top10_share"], 0, "%")),
        trow("Exits: 40% stop / 10% stop / delisted / end of test",
             lambda x: f"{t(x)['exit_40']} / {t(x)['exit_10']} / {t(x)['exit_delisted']} / {t(x)['exit_end']}"),
        trow("Stops tightened to 10% / loosened back to 40% (times)", lambda x: f"{t(x)['tightened']} / {t(x)['loosened']}"),
        trow("Signals / entries / no free slot / no cash",
             lambda x: f"{t(x)['signals']:,} / {t(x)['entries']:,} / {t(x)['no_slot']:,} / {t(x)['no_cash']:,}"),
    ]
    out += [table(["Combined (2016–present)", *RULE_NAMES.values()], rows), ""]

    years = list(mc["ratchet/combined"]["yearly_median"])
    out += ["## Yearly returns", "", f"Median of the {n} random runs (combined period), with the 5th–95th percentile.", ""]
    rows = []
    for y in years:
        rows.append([y] + [f"{f(mc[f'{r}/combined']['yearly_median'][y])}% ({f(mc[f'{r}/combined']['yearly_p5'][y], 0)} to "
                           f"{f(mc[f'{r}/combined']['yearly_p95'][y], 0)})" for r in RULE_NAMES]
                    + [f"{f(res['spy_yearly'][y])}%"])
    out += [table(["Year", *RULE_NAMES.values(), "SPY with dividends"], rows), ""]
    out += [method(res)]
    return "\n".join(out) + "\n"


def findings(res: dict) -> str:
    mc, spy, fx, paired, n = res["mc"], res["spy"], res["fixed"], res["paired"], res["seeds"]
    a, z = mc["ratchet/combined"], mc["recompute/combined"]
    ai, ao = mc["ratchet/in_sample"], mc["ratchet/out_of_sample"]
    med = fx["wtt_ratchet_random_median"]["trades"]
    mcurve = fx["wtt_ratchet_random_median"]["curve"]
    y = a["yearly_median"]
    sy = res["spy_yearly"]
    filt = res["index_filter"]["combined"]
    momentum = [fx[k]["periods"]["combined"] for k in ("wtt_ratchet_ret63", "wtt_recompute_ret63",
                                                      "wtt_ratchet_roc", "wtt_recompute_roc")]
    mom_trades = [fx[k]["trades"] for k in ("wtt_ratchet_ret63", "wtt_ratchet_roc")]
    beat_total = round(a["beat_spy_total"] * n / 100)
    beat_price = round(a["beat_spy_price"] * n / 100)
    items = [
        f"**Neither version beat SPY from 2016.** With the stop as described, the median of {n} random selections "
        f"made {f(a['cagr_p50'])}%/yr (5th–95th percentile {f(a['cagr_p5'])}–{f(a['cagr_p95'])}%) with a "
        f"{f(a['max_drawdown_p50'], 0)}% maximum drawdown ({f(a['max_drawdown_p5'], 0)}–{f(a['max_drawdown_p95'], 0)}%). "
        f"SPY made {f(spy['combined']['cagr'])}%/yr with dividends and {f(spy['combined']['cagr_price'])}% on price "
        f"alone, with a {f(spy['combined']['max_drawdown'], 0)}% drawdown. {beat_total} of {n} runs beat SPY with "
        f"dividends; {beat_price} beat its price return, the like-for-like comparison, since trade returns here "
        "exclude dividends.",
        f"**It worked until 2021 and stalled after.** In 2016–2021 the median run made {f(ai['cagr_p50'])}%/yr against "
        f"SPY's {f(spy['in_sample']['cagr'])}% ({f(spy['in_sample']['cagr_price'])}% price only), and "
        f"{f(ai['beat_spy_price'], 0)}% of runs beat SPY's price return. From 2022 it made {f(ao['cagr_p50'])}%/yr "
        f"against {f(spy['out_of_sample']['cagr'])}%, and {f(100 - ao['positive'], 0)}% of runs lost money. The best "
        f"years were 2020 ({f(y['2020'], 0, '%', True)}) and 2017 ({f(y['2017'], 0, '%', True)}). It was flat in "
        f"2021 ({f(y['2021'], 0, '%', True)} vs SPY {f(sy['2021'], 0, '%', True)}), fell {f(-y['2022'], 0)}% in 2022 "
        f"and lost {f(-y['2025'], 0)}% in 2025, when SPY made {f(sy['2025'], 0)}%.",
        f"**The recomputed stop made no reliable difference.** Median {f(z['cagr_p50'])}%/yr with a "
        f"{f(z['max_drawdown_p50'], 0)}% drawdown. Paired by random seed, it did better in "
        f"{f(paired['combined']['recompute_better'], 0)}% of runs (median {f(paired['combined']['median_diff'], 1, sign=True)} "
        f"pts/yr). It helped in 2016–2021 ({f(paired['in_sample']['recompute_better'], 0)}% of runs, "
        f"{f(paired['in_sample']['median_diff'], 1, sign=True)} pts) and hurt from 2022 "
        f"({f(paired['out_of_sample']['recompute_better'], 0)}%, {f(paired['out_of_sample']['median_diff'], 1, sign=True)} "
        "pts). Letting the stop widen again paid in the steadier trends before 2022 and cost in the choppier market "
        "since.",
        f"**In practice it is a 10% trailing-stop system.** SPY closed below its 10-week average in "
        f"{f(filt['down_pct'], 0)}% of weeks, in {filt['spells']} separate spells (about four a year), and each "
        f"one tightens every open stop to 10%. In the median run, {med['exit_10']} of {med['trades']} exits were "
        f"at the 10% stop and {med['exit_40']} at the 40% stop. The 40% stop is the one in force while the index is up, "
        "but almost every position meets a down week before it falls that far.",
        f"**The choice between signals decides whether it makes or loses money.** On average "
        f"{med['signals'] / (filt['weeks'] * (1 - filt['down_pct'] / 100)):.0f} liquid US stocks met the entry "
        f"rules in a week with the filter up, against a handful of free slots: the median run took {med['entries']} "
        f"of {med['signals']:,} weekly signals. Ranked by 63-day return or 20-week rate of change, "
        f"the system lost {f(-max(m['cagr'] for m in momentum), 0)}–{f(-min(m['cagr'] for m in momentum), 0)}%/yr "
        f"with {f(min(m['max_drawdown'] for m in momentum), 0)}–{f(max(m['max_drawdown'] for m in momentum), 0)}% "
        "drawdowns. Those rankings fill the slots with the most extended stocks, above all in the early-2021 "
        "speculative run (fuel-cell, EV-charging, crypto-mining and small biotech names, many bought within weeks of "
        f"their peak). That lifts the average loss from {f(med['avg_loss_pct'], 1)}% to "
        f"{f(mom_trades[0]['avg_loss_pct'], 1)}% and the count of losses worse than -40% from "
        f"{med['losses_over_40']} to {mom_trades[0]['losses_over_40']}. The book gives no ranking rule. A forum "
        "member reported the same sensitivity on the Russell 3000 in 2020.",
        f"**Trade profile (median run, stop as described):** {f(med['win_rate'], 0)}% winners averaging "
        f"{f(med['avg_win_pct'], 0, '%', True)} against losers averaging {f(med['avg_loss_pct'], 0, '%', True)} "
        f"(payoff {f(med['payoff'], 1)}). The average trade made {f(med['expectancy_pct'], 1, '%', True)}; the median "
        f"trade lost {f(-med['median_pct'], 1)}%. Winners were held {f(med['hold_win_weeks'], 0)} weeks and losers "
        f"{f(med['hold_loss_weeks'], 0)}. The top 10% of trades made {f(med['top10_share'], 0)}% of the net profit. "
        f"Beta to SPY {f(mcurve['beta'], 2)}, correlation {f(mcurve['corr_spy'], 2)}, about "
        f"{f(a['exposure_avg_invested_pct_median'], 0)}% invested on average.",
        "**Published results look better but cover other ground.** QuantifiedStrategies reports 18%/yr with a 33% "
        "drawdown on the Russell 3000, and Radge's turnkey code was sold on a CAGR/drawdown ratio of 1.16 for "
        "1995–2012. Both use other "
        "periods and a broader universe, and their selection rules are not given. The closest slice here, "
        f"2016–2021, made {f(ai['cagr_p50'])}%/yr with a {f(ai['max_drawdown_p50'], 0)}% drawdown, and that period "
        "is flattered by thin delisting data.",
    ]
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


def method(res: dict) -> str:
    return "\n".join([
        "## Rules and method", "",
        "Rules (Nick Radge, *Weekend Trend Trader*, 2012), checked on weekly closes. A week's close is the close "
        "of its last session; orders go in at the next session's open.",
        "- Index filter: SPY's weekly close above the mean of its last 10 weekly closes (SPY stands in for the "
        "S&P 500).",
        "- Entry: the stock's weekly close is at least the highest of its previous 20 weekly closes, and its 20-week "
        "rate of change is at least 30%. No new entries while the index filter is down.",
        "- Stop: 40% below the highest weekly close since the signal week while the index filter is up, 10% below "
        "once it is down. A weekly close under the stop sells at the next open. No intraday stops.",
        "  - As described (the book; confirmed on the AmiBroker forum): the stop never moves down. After a down "
        "week it stays at the 10% level until 40% below a new high passes it.",
        "  - Recomputed: the stop is recomputed each week from the highest close with that week's percentage, so "
        "it loosens back to 40% when the index recovers.",
        "- Size: 5% of equity (at the previous close) per position, at most 20 positions, no margin. An entry that "
        "needs more than the cash left is shrunk to it; with no cash left, the signal is skipped.",
        "",
        "Choices where the book is silent or the data differs:",
        f"- Universe: common stocks of US companies within the ground rules' liquidity floor (as-traded close of "
        f"$5 or more, 20-day average dollar volume of $20M or more at the signal close). {res['foreign_left_out']:,} "
        "stocks of foreign companies (by headquarters country, or an ADR flag) are left out, as the Russell 3000 leaves them "
        "out. Radge designed the system on the Russell 3000, which also holds smaller and less liquid stocks than "
        "this floor admits, but not the micro-caps that briefly clear it in a volume spike.",
        "- Ranking: the book gives no rule for more signals than free slots. Random order is the headline "
        "(Radge recommends Monte Carlo resampling to remove selection bias); the ground rules' 63-session return "
        "and the 20-week rate of change are fixed alternatives.",
        "- Costs: slippage 0.10% a side, 0.25% under $20 as traded. Trade returns exclude dividends; idle cash "
        "earns nothing. About 17% of equity sat in cash on average; at T-bill rates (2.4% a year on average, 4.1% "
        "from 2022) that would have added roughly 0.4% a year, 0.7% from 2022.",
        "- Periods: in-sample 2016–2021, out-of-sample 2022 onward, combined; each a separate run from $100,000, "
        "closing open positions at the end. Nothing was tuned, so the split is only a robustness check.",
        "- Delisted stocks: the price data's coverage is thin before 2021 (28 delisted in 2019, 9 in 2020 with prices), so "
        "2016–2020 is flattered by survivorship. Delisted positions exit at their last close.",
        "",
        "Files: `montecarlo.csv` (every random run), `curves_combined.csv`, `results.json`; harness outputs with "
        "trade lists in `output/wtt_*` (the median random run of each rule and the fixed rankings).",
    ])


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
