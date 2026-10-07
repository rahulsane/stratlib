"""A QuantifiedStrategies-style Weekend Trend Trader on the Russell 3000 and on the S&P 500 + MidCap 400.
Findings: output/wttqs/report.md.

QuantifiedStrategies' rules are members-only. This reconstructs their version from what they disclose:
- Entry: WTT's three rules (they list three: breakout, momentum, market regime): a 20-week closing high, a 20-week
  rate of change of 30% or more, SPY above its 10-week average; buy at the next open.
- Exit: "a wide trailing stop": 40% below the highest weekly close, checked on weekly closes, never tightened
  (the index filter only gates entries). In wtt_gap.py this reproduced their 65-121-week holding periods.
- 10 positions of 10%; no commissions or slippage (a with-costs run uses the ground rules' slippage).
- Universe: index members at the signal week, with no liquidity floor beyond an as-traded close of $1, since
  they used index constituents (Norgate). Russell 3000 from archived iShares holdings (wtt_russell.py); S&P 500 from
  FMP's change log and MidCap 400 from Wikipedia and IJH (wtt_universe.py).
- Ranking: unknown. Random selection (SEEDS runs) is the headline; their worked example ranks by 3-month relative
  strength, run as the 63-session return, highest first.
Benchmarks: SPY (with dividends and on price), IWV (Russell 3000 ETF, price), and WTT as written on the same
universes (20 x 5%, the tightening stop, slippage).

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_qs.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_qs.py --report
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date

import numpy as np

import benchmarks
import panel as P
import run_wtt as W
import wtt_russell
import wtt_universe as U
from stratlib.app import open_context
from stratlib.sim.panel import FIELDS, Panel, build
from engine import OUTPUT, PERIODS, PERIOD_TITLES, metrics, run_test, simulate, spy_series
from strategies.weekend_trend import WeekendTrend, week_ends

OUT = OUTPUT / "wttqs"
PANEL_PATH = P.CACHE / f"panel_broad_{W.THROUGH}.npz"
MASK_PATH = P.CACHE / f"wttqs_members_{W.THROUGH}.npz"
MIN_PRICE = 1.0
SEEDS = 200
WORKERS = 10   # the broad panel is about 1 GB per process
UNIVERSES = {"r3000": "Russell 3000", "sp900": "S&P 500 + MidCap 400"}
QS_PARAMS = {"stop_rule": "ratchet", "stop_up_pct": 40.0, "stop_down_pct": 40.0, "position_pct": 10.0}
QS_RULES = replace(W.RULES, max_positions=10, max_position_pct=10.0, slippage_pct=0.0, slippage_low_price_pct=0.0)
CONFIGS = {   # name -> (params, rules, label)
    "qs": (QS_PARAMS, QS_RULES, "QuantifiedStrategies-style, no costs"),
    "qs_costs": (QS_PARAMS, replace(QS_RULES, slippage_pct=W.RULES.slippage_pct,
                                    slippage_low_price_pct=W.RULES.slippage_low_price_pct),
                 "QuantifiedStrategies-style, with slippage"),
    "wtt": ({"stop_rule": "ratchet"}, W.RULES, "WTT as written (20 x 5%, tightening stop, slippage)"),
}
f, table, pct = W.f, W.table, W.pct


# ----------------------------------------------------------------------
# Data: a panel without the dollar-volume floor, limited to stocks ever in these indexes


def subset(p: Panel, keep: np.ndarray) -> Panel:
    return Panel(dates=p.dates, symbols=p.symbols[keep], kind=p.kind[keep], until=p.until[keep],
                 factor=p.factor[:, keep], notes=p.notes, min_price=MIN_PRICE, min_dollar_volume=0.0,
                 **{k: getattr(p, k)[:, keep] for k in FIELDS})


def read_panel(path) -> Panel:
    data = np.load(path, allow_pickle=False)
    return Panel(dates=data["dates"], symbols=data["symbols"], kind=data["kind"], until=data["until"],
                 factor=data["factor"], notes=json.loads(str(data["notes"])), min_price=MIN_PRICE, min_dollar_volume=0.0,
                 **{k: data[k] for k in FIELDS})


def full_panel() -> Panel:
    """Every stock with prices (no dollar-volume floor), cached; the runs use the members-only subset."""
    path = P.CACHE / f"panel_broadfull_{W.THROUGH}.npz"
    if path.exists():
        return read_panel(path)
    p = build(W.THROUGH, include_etfs=False, min_price=MIN_PRICE, min_dollar_volume=0.0)
    P.save(p, path)
    return p


def load_data() -> tuple[Panel, dict]:
    if not (PANEL_PATH.exists() and MASK_PATH.exists()):
        full = full_panel()
        columns = U.Columns(full)
        r3000 = wtt_russell.membership(full, columns)
        sp = U.membership(full)
        since = full.session_index("2015-12-01")
        ever = (r3000["r3000"] | sp["sp500"] | sp["sp400"])[since:].any(axis=0)
        ever |= np.isin(full.symbols, ["SPY", "QQQ"])
        keep = np.flatnonzero(ever)
        p = subset(full, keep)
        P.save(p, PANEL_PATH)
        info = {"russell_sizes": r3000["sizes"], "russell_matched": r3000["matched"],
                "sp_agreement": sp["agreement"], "columns_full": int(full.close.shape[1]), "columns_kept": len(keep)}
        np.savez_compressed(MASK_PATH, r3000=r3000["r3000"][:, keep], sp500=sp["sp500"][:, keep],
                            sp400=sp["sp400"][:, keep], info=np.array(json.dumps(info)))
        del full
    p = read_panel(PANEL_PATH)
    m = np.load(MASK_PATH)
    return p, {"r3000": m["r3000"], "sp500": m["sp500"], "sp400": m["sp400"], "info": json.loads(str(m["info"]))}


_W: dict = {}


def _init() -> None:
    p, m = load_data()
    b = benchmarks.load()
    WeekendTrend.members = {k: m[k] for k in ("r3000", "sp500", "sp400")}
    _W.update(panel=p, bench=b, info=m["info"], spy={k: spy_series(p, b["spy_dividends"], s, e or str(p.dates[-1]))
                                                     for k, (s, e) in PERIODS.items()})


def mc_task(task: tuple) -> dict:
    universe, config, period, seed = task
    p, b = _W["panel"], _W["bench"]
    params, rules, _ = CONFIGS[config]
    start, end = PERIODS[period]
    s = WeekendTrend()
    s.setup(p, {**params, "universe": universe, "rank": "random", "seed": seed})
    run = simulate(p, s, start, end, rules)
    m = metrics(run, _W["spy"][period], b["tbill3m"])
    row = {"universe": universe, "config": config, "period": period, "seed": seed,
           **{k: float(m[k]) for k in W.MC_KEYS}, "weeks": float(np.mean([t.holding_days / 7 for t in run["trades"]])),
           "yearly": m["yearly"]}
    if period == "combined":
        row["equity"] = run["equity"].astype(np.float32)
    return row


def etf_stats(symbol: str, p: Panel) -> dict:
    """Price-only buy-and-hold of an ETF from the app's database, per period."""
    ctx = open_context()
    try:
        bars = {b.date: b.close for b in ctx.store.price_history(symbol, since="2015-12-01")}
    finally:
        ctx.close()
    out = {}
    for period, (start, end) in PERIODS.items():
        days = [d for d in sorted(bars) if start <= d <= (end or str(p.dates[-1]))]
        c = np.array([bars[d] for d in days])
        years = (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25
        out[period] = {"cagr_price": 100 * ((c[-1] / c[0]) ** (1 / years) - 1),
                       "max_drawdown": 100 * float((1 - c / np.maximum.accumulate(c)).max())}
    return out


def universe_stats(p: Panel, m: dict) -> dict:
    ends = np.flatnonzero(week_ends(p.dates))
    ends = ends[p.dates[ends] >= "2016-01-01"]
    out = {}
    for u in UNIVERSES:
        mask = (m["sp500"] | m["sp400"]) if u == "sp900" else m[u]
        n = mask[ends].sum(axis=1)
        eligible = (mask & p.eligible & (p.kind == "stock")[None, :])[ends].sum(axis=1)
        out[u] = {"members_median": int(np.median(n)), "members_min": int(n.min()), "members_max": int(n.max()),
                  "eligible_median": int(np.median(eligible))}
    return out


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    _init()
    p, b, spy = _W["panel"], _W["bench"], _W["spy"]
    print(f"panel {p.close.shape}, {time.time() - t0:.0f} s")
    tasks = [(u, c, period, seed) for u in UNIVERSES for c in ("qs", "qs_costs") for period in PERIODS
             for seed in range(SEEDS)]
    tasks += [(u, "wtt", "combined", seed) for u in UNIVERSES for seed in range(SEEDS)]
    with ProcessPoolExecutor(WORKERS, initializer=_init) as pool:
        rows = list(pool.map(mc_task, tasks, chunksize=8))
    print(f"Monte Carlo: {len(rows)} runs, {time.time() - t0:.0f} s")
    with (OUT / "montecarlo.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["universe", "config", "period", "seed", *W.MC_KEYS, "weeks_held"])
        for r in rows:
            w.writerow([r["universe"], r["config"], r["period"], r["seed"], *(round(r[k], 4) for k in W.MC_KEYS),
                        round(r["weeks"], 1)])

    base = json.loads((W.OUT / "results.json").read_text(encoding="utf-8"))
    spy_stats, spy_yearly = base["spy"], base["spy_yearly"]
    mc, median_seed = {}, {}
    for (u, c, period) in sorted({(r["universe"], r["config"], r["period"]) for r in rows}):
        sub = [r for r in rows if r["universe"] == u and r["config"] == c and r["period"] == period]
        cg = np.array([r["cagr"] for r in sub])
        mc[f"{u}/{c}/{period}"] = {
            **{f"{k}_p{q}": pct([r[k] for r in sub], q) for k in ("cagr", "max_drawdown", "sharpe") for q in (5, 50, 95)},
            **{f"{k}_median": pct([r[k] for r in sub], 50) for k in (*W.MC_KEYS, "weeks")},
            "beat_spy_total": 100 * float((cg > spy_stats[period]["cagr"]).mean()),
            "beat_spy_price": 100 * float((cg > spy_stats[period]["cagr_price"]).mean()),
            "positive": 100 * float((cg > 0).mean()),
            "yearly_median": {y: pct([r["yearly"][y] for r in sub], 50) for y in sub[0]["yearly"]},
        }
        if period == "combined":
            ordered = sorted(sub, key=lambda r: r["cagr"])
            median_seed[f"{u}/{c}"] = ordered[len(ordered) // 2]["seed"]

    fixed = {}
    for u in UNIVERSES:
        for name, rank, seed in ((f"wttqs_{u}_random_median", "random", median_seed[f"{u}/qs"]),
                                 (f"wttqs_{u}_ret63", "ret63", 0)):
            params = {**QS_PARAMS, "universe": u, "rank": rank, "seed": seed}
            desc = (f"QuantifiedStrategies-style Weekend Trend Trader on {UNIVERSES[u]} members (point in time): WTT "
                    "entries, a 40% trailing stop that never tightens, 10 positions of 10%, no costs. Ranking: "
                    + (f"random, seed {seed} (the median of {SEEDS} random runs, combined period)" if rank == "random"
                       else "63-session return, highest first") + ".")
            summary = run_test(p, name, WeekendTrend, params, b, rules=QS_RULES, description=desc)
            fixed[name] = {"universe": u, "rank": rank, "seed": seed,
                           "periods": {k: {mm: float(v[mm]) for mm in W.MC_KEYS} for k, v in summary["results"].items()},
                           **{k: v for k, v in W.detail(p, b, spy, params, QS_RULES).items() if k != "equity"}}
            print(name, {k: round(v["cagr"], 1) for k, v in fixed[name]["periods"].items()})

    start = PERIODS["combined"][0]
    dates = p.dates[p.session_index(start):]
    cols = {"spy_total": W.RULES.capital * spy["combined"]["total"],
            "spy_price": W.RULES.capital * spy["combined"]["price"]}
    for u in UNIVERSES:
        eq = np.array([r["equity"] for r in rows if r["universe"] == u and r["config"] == "qs"
                       and r["period"] == "combined"], dtype=float)
        dd = 1 - eq / np.maximum.accumulate(eq, axis=1)
        for q in (5, 50, 95):
            cols[f"{u}_p{q}"] = np.percentile(eq, q, axis=0)
            cols[f"{u}_dd_p{q}"] = -100 * np.percentile(dd, 100 - q, axis=0)
        s = WeekendTrend()
        s.setup(p, {**QS_PARAMS, "universe": u, "rank": "ret63"})
        cols[f"{u}_ret63"] = simulate(p, s, start, None, QS_RULES)["equity"]
    with (OUT / "curves_combined.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", *cols])
        for i, d in enumerate(dates):
            w.writerow([d, *(round(float(v[i]), 2) for v in cols.values())])

    results = {"data_through": str(p.dates[-1]), "seeds": SEEDS, "spy": spy_stats, "spy_yearly": spy_yearly,
               "iwv": etf_stats("IWV", p), "mc": mc, "median_seed": median_seed, "fixed": fixed,
               "universe": universe_stats(p, WeekendTrend.members),
               "membership": _W["info"], "gap": json.loads((W.OUT / "gap.json").read_text(encoding="utf-8"))}
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    (OUT / "report.md").write_text(report(results), encoding="utf-8")
    print(f"done in {time.time() - t0:.0f} s")


PERIOD_ORDER = ["combined", "in_sample", "out_of_sample"]
QS_PUBLISHED = {"r3000": "18% (33% drawdown; 1,165 trades held 101 weeks; 49% winners, +37% / -15%)",
                "sp900": "S&P 500 19.9% (43% drawdown), MidCap 400 22.9% (58%)"}


def findings(res: dict) -> str:
    mc, spy, fx, iwv = res["mc"], res["spy"], res["fixed"], res["iwv"]
    r, s = mc["r3000/qs/combined"], mc["sp900/qs/combined"]
    rc, sc = mc["r3000/qs_costs/combined"], mc["sp900/qs_costs/combined"]
    rw, sw = mc["r3000/wtt/combined"], mc["sp900/wtt/combined"]
    ri, ro = mc["r3000/qs/in_sample"], mc["r3000/qs/out_of_sample"]
    si, so = mc["sp900/qs/in_sample"], mc["sp900/qs/out_of_sample"]
    r63, s63 = fx["wttqs_r3000_ret63"]["periods"], fx["wttqs_sp900_ret63"]["periods"]
    rm = fx["wttqs_r3000_random_median"]
    ry, sy = r["yearly_median"], s["yearly_median"]
    uni = res["universe"]

    def better(a, b):
        return "better" if a > b else "worse"

    items = [
        f"**Neither universe came close to the published figures, or to SPY.** Russell 3000: the median of "
        f"{res['seeds']} random selections made {f(r['cagr_p50'])}%/yr (5th–95th percentile {f(r['cagr_p5'])}–"
        f"{f(r['cagr_p95'])}%) with a {f(r['max_drawdown_p50'], 0)}% maximum drawdown. S&P 500 + MidCap 400: "
        f"{f(s['cagr_p50'])}% ({f(s['cagr_p5'])}–{f(s['cagr_p95'])}%) with {f(s['max_drawdown_p50'], 0)}%. "
        "QuantifiedStrategies reports 18% on the Russell 3000 and 19.9% (S&P 500) to 22.9% (MidCap 400) for "
        f"1990–2025. SPY made {f(spy['combined']['cagr'])}%/yr with dividends ({f(spy['combined']['cagr_price'])}% "
        f"on price) and IWV, the Russell 3000 ETF, {f(iwv['combined']['cagr_price'])}% on price. "
        f"{f(r['beat_spy_price'], 0)}% of Russell 3000 runs and {f(s['beat_spy_price'], 1)}% of S&P runs beat SPY's "
        "price return.",
        f"**With 10 positions, which stocks get picked matters as much as the rules.** The Russell 3000 runs span "
        f"{f(r['cagr_p95'] - r['cagr_p5'], 0)} points between the 5th and 95th percentiles. The 3-month ranking from "
        f"their worked example made {f(r63['combined']['cagr'])}% on the Russell 3000 "
        f"({f(r63['combined']['max_drawdown'], 0)}% drawdown), {better(r63['combined']['cagr'], r['cagr_p50'])} than "
        f"the random median, but {f(s63['combined']['cagr'])}% on the S&P indexes "
        f"({f(s63['combined']['max_drawdown'], 0)}%). Run separately, the S&P halves made "
        f"{f(s63['in_sample']['cagr'])}% and {f(s63['out_of_sample']['cagr'])}%; the combined run differs because it "
        "carries positions across 2022. It is one path, not a property of the ranking.",
        f"**It matched their holding periods, not their trades.** Median holding {f(r['weeks_median'], 0)} weeks on the "
        f"Russell 3000 and {f(s['weeks_median'], 0)} on the S&P indexes (theirs: 101 and 65–121). But it stayed "
        f"{f(r['exposure_avg_invested_pct_median'], 0)}–{f(s['exposure_avg_invested_pct_median'], 0)}% invested "
        f"(theirs 78–86%), and its trades averaged {f(r['avg_win_pct_median'], 0, '%', True)} / "
        f"{f(r['avg_loss_pct_median'], 0, '%', True)} on the Russell 3000 against their +37% / -15%. Their exit probably cuts "
        "losers sooner and move to cash more often, perhaps on the market filter. A 40% stop that never tightens "
        f"comes close to holding 10 momentum stocks for years: beta {f(rm['curve']['beta'], 2)} to SPY in the median "
        "Russell 3000 run.",
        f"**WTT as written did as well or better on the same universes.** Russell 3000 {f(rw['cagr_p50'])}%/yr with a "
        f"{f(rw['max_drawdown_p50'], 0)}% drawdown, against {f(r['cagr_p50'])}% and {f(r['max_drawdown_p50'], 0)}%; "
        f"S&P indexes {f(sw['cagr_p50'])}% and {f(sw['max_drawdown_p50'], 0)}%, against {f(s['cagr_p50'])}% and "
        f"{f(s['max_drawdown_p50'], 0)}%. Costs hardly matter for the reconstruction (about "
        f"{f(r['trades_median'] / 10.7, 0)} trades a year): with slippage it made {f(rc['cagr_p50'])}% and "
        f"{f(sc['cagr_p50'])}%.",
        f"**Both periods trailed SPY.** Russell 3000 {f(ri['cagr_p50'])}%/yr in 2016–2021 and {f(ro['cagr_p50'])}% from "
        f"2022; S&P indexes {f(si['cagr_p50'])}% and {f(so['cagr_p50'])}%; SPY {f(spy['in_sample']['cagr'])}% and "
        f"{f(spy['out_of_sample']['cagr'])}%. The worst years were 2022 ({f(ry['2022'], 0, '%', True)} and "
        f"{f(sy['2022'], 0, '%', True)}) and 2018 ({f(ry['2018'], 0, '%', True)} and {f(sy['2018'], 0, '%', True)}).",
        f"**Russell 3000 coverage is thinner early on.** A typical week had {uni['r3000']['members_median']:,} members "
        f"with prices ({uni['r3000']['members_min']:,} at the low, in 2016), out of about 3,000: small companies "
        "delisted before 2021 mostly have no price history here, which flatters 2016–2020 more for the Russell 3000 "
        "than for the S&P test.",
    ]
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))


def report(res: dict) -> str:
    mc, spy, fx, iwv = res["mc"], res["spy"], res["fixed"], res["iwv"]
    n = res["seeds"]
    title = {p: PERIOD_TITLES[p] for p in PERIOD_ORDER}
    out = ["# A QuantifiedStrategies-style Weekend Trend Trader: Russell 3000 and S&P 500 + MidCap 400", "",
           f"Data through {res['data_through']}.", "",
           "QuantifiedStrategies' rules are members-only; this is a reconstruction from what they disclose (see "
           "*Rules* below and the *Why the published 18% differs* note in [*Nick Radge's Weekend Trend Trader: two stop rules*](/reports?report=weekend-trend-trader)).", "",
           "## Findings", "", findings(res), "", "![Growth of $100,000 and drawdowns](equity_drawdown.png)", ""]
    rows = []
    for period in PERIOD_ORDER:
        for u in UNIVERSES:
            for c in ("qs", "qs_costs", "wtt"):
                key = f"{u}/{c}/{period}"
                if key not in mc:
                    continue
                m = mc[key]
                rows.append([title[period], UNIVERSES[u], CONFIGS[c][2],
                             f"{f(m['cagr_p5'])}% / **{f(m['cagr_p50'])}%** / {f(m['cagr_p95'])}%",
                             f"{f(m['max_drawdown_p50'], 0)}%", f(m["sharpe_p50"], 2), f(m["weeks_median"], 0),
                             f"{f(m['beat_spy_total'], 0)}% / {f(m['beat_spy_price'], 0)}%"])
        rows.append([title[period], "SPY buy-and-hold", "",
                     f"**{f(spy[period]['cagr'])}%** with dividends, {f(spy[period]['cagr_price'])}% price",
                     f"{f(spy[period]['max_drawdown'], 0)}%", "", "", ""])
        rows.append([title[period], "IWV buy-and-hold (Russell 3000)", "", f"{f(iwv[period]['cagr_price'])}% price",
                     f"{f(iwv[period]['max_drawdown'], 0)}%", "", "", ""])
    out += ["## Random selection", "",
            f"{n} runs each; 5th / median / 95th percentile CAGR. WTT as written was run on the combined period only.", "",
            table(["Period", "Universe", "Version", "CAGR", "Max drawdown (median)", "Sharpe (median)",
                   "Weeks held (median)", "Runs beating SPY: with dividends / price"], rows), "",
            "Trade returns exclude dividends and idle cash earns nothing, so SPY's price return is the like-for-like "
            "figure.", ""]

    rows = [[UNIVERSES[u], rank_label] + [f"{f(fx[name]['periods'][p]['cagr'])}% / "
                                          f"{f(fx[name]['periods'][p]['max_drawdown'], 0)}%" for p in PERIOD_ORDER]
            + [QS_PUBLISHED[u]]
            for u in UNIVERSES for name, rank_label in ((f"wttqs_{u}_ret63", "3-month return, highest first"),
                                                        (f"wttqs_{u}_random_median", "random (median run)"))]
    out += ["## Fixed runs against the published figures", "", "CAGR / max drawdown, no costs.", "",
            table(["Universe", "Ranking", *[title[p] for p in PERIOD_ORDER], "QuantifiedStrategies, 1990–2025"], rows),
            ""]

    names = ["wttqs_r3000_random_median", "wttqs_r3000_ret63", "wttqs_sp900_random_median", "wttqs_sp900_ret63"]
    heads = ["Russell 3000, median random run", "Russell 3000, 3-month ranking", "S&P 900, median random run",
             "S&P 900, 3-month ranking"]
    t = lambda x: x["trades"]
    c = lambda x: x["curve"]
    m = lambda x: x["metrics"]

    def trow(label, fn):
        return [label] + [fn(fx[nm]) for nm in names]

    rows = [
        trow("CAGR / max drawdown", lambda x: f"{f(m(x)['cagr'])}% / {f(m(x)['max_drawdown'], 0)}%"),
        trow("Sharpe / volatility", lambda x: f"{f(m(x)['sharpe'], 2)} / {f(c(x)['vol'], 0)}%"),
        trow("Worst month / longest time under a previous high",
             lambda x: f"{f(c(x)['worst_month'])}% / {f(c(x)['longest_underwater_months'], 0)} months"),
        trow("Beta / correlation to SPY", lambda x: f"{f(c(x)['beta'], 2)} / {f(c(x)['corr_spy'], 2)}"),
        trow("Average invested / positions held", lambda x: f"{f(m(x)['exposure_avg_invested_pct'], 0)}% / {f(t(x)['avg_held'], 1)}"),
        trow("Trades / per year", lambda x: f"{t(x)['trades']:,} / {f(t(x)['per_year'], 0)}"),
        trow("Win rate", lambda x: f"{f(t(x)['win_rate'])}%"),
        trow("Average win / average loss", lambda x: f"{f(t(x)['avg_win_pct'], 1, '%', True)} / {f(t(x)['avg_loss_pct'], 1, '%', True)}"),
        trow("Average / median trade", lambda x: f"{f(t(x)['expectancy_pct'], 1, '%', True)} / {f(t(x)['median_pct'], 1, '%', True)}"),
        trow("Profit factor", lambda x: f(t(x)["profit_factor"], 2)),
        trow("Average holding: winners / losers", lambda x: f"{f(t(x)['hold_win_weeks'], 0)} / {f(t(x)['hold_loss_weeks'], 0)} weeks"),
        trow("Losses worse than -40%", lambda x: f"{t(x)['losses_over_40']}"),
        trow("Top 10% of trades, share of profit", lambda x: f(t(x)["top10_share"], 0, "%")),
        trow("Exits: stop / delisted / end of test",
             lambda x: f"{t(x)['exit_40']} / {t(x)['exit_delisted']} / {t(x)['exit_end']}"),
        trow("Signals / entries / no free slot / no cash",
             lambda x: f"{t(x)['signals']:,} / {t(x)['entries']:,} / {t(x)['no_slot']:,} / {t(x)['no_cash']:,}"),
    ]
    out += ["## Trade detail (combined period, no costs)", "", table(["", *heads], rows), ""]
    years = list(mc["r3000/qs/combined"]["yearly_median"])
    rows = [[y] + [f"{f(mc[f'{u}/qs/combined']['yearly_median'][y])}%" for u in UNIVERSES]
            + [f"{f(res['spy_yearly'][y])}%"] for y in years]
    out += ["## Yearly returns", "", "Median random run, no costs.", "",
            table(["Year", *UNIVERSES.values(), "SPY with dividends"], rows), ""]
    out += [rules_section(res)]
    return "\n".join(out) + "\n"


def rules_section(res: dict) -> str:
    info = res["membership"]
    sizes, matched = info["russell_sizes"], info["russell_matched"]
    by_fund: dict[str, list] = {}
    for k, v in sizes.items():
        fund = k.split()[0]
        by_fund.setdefault(fund, []).append(100 * matched[k] / v)
    cover = "; ".join(f"{fund} {len(v)} snapshots, {min(v):.0f}–{max(v):.0f}% matched" for fund, v in by_fund.items())
    return "\n".join([
        "## Rules", "",
        "What QuantifiedStrategies discloses, and how it is reproduced here:",
        "- Entry: they list three rules (breakout, momentum, market regime) and buy at Monday's open; used here: "
        "WTT's 20-week closing high, 20-week rate of change of 30% or more, and SPY above its 10-week average "
        "(they use each index's own trend; SPY stands in for both universes).",
        "- Exit: \"a wide trailing stop\" with average holdings of 65–121 weeks; used here: 40% below the highest "
        "weekly close, checked on weekly closes and never tightened. Sold at the next open.",
        "- 10 positions of 10%; no commissions or slippage (a second run adds slippage of 0.10% a "
        "side, 0.25% under $20).",
        "- Universe: index members at the signal week; no liquidity floor beyond an as-traded close of $1. They used "
        "Norgate's historical constituents from 1990; this test covers 2016–2026.",
        "- Ranking: not disclosed. Random selection is the headline; their worked example ranks by 3-month relative "
        "strength, run here as the 63-session return, highest first.",
        "- Not reproduced: their period (1990 onward), their exact exit, dividends (trade returns here are price "
        "only) and interest on idle cash.",
        "",
        "Membership:",
        f"- Russell 3000: the union of IWB (Russell 1000), IWM (Russell 2000) and IWV (Russell 3000) holdings saved by "
        f"the Wayback Machine, any from the previous 190 days ({cover}). Unmatched names are mostly companies "
        "acquired before 2021 that have no price history here, and REITs and trusts, which the stock universe here "
        "leaves out.",
        "- S&P 500 and MidCap 400: as in [*Weekend Trend Trader on the S&P 500 and MidCap 400*](/reports?report=weekend-trend-trader-sp900).",
        f"- The broad price panel holds {info['columns_kept']:,} stocks that were ever members (of "
        f"{info['columns_full']:,} with prices).",
        "- Survivorship: delisted coverage is thin before 2021, so 2016–2020 is flattered.",
        "",
        "Files: `montecarlo.csv`, `curves_combined.csv`, `results.json`; trade lists for the median random runs and "
        "the 3-month ranking are under *Compare variations*.",
    ])


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
