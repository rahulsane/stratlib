"""Weekend Trend Trader on S&P 500 and S&P MidCap 400 members (point in time). Findings: output/wtt900/report.md.

The rules, stop variants, sizing, costs and random-selection method are those of run_wtt.py; only the universe
changes. A stock may be bought when it is in the index at the signal week's close (wtt_universe.py builds the
membership); positions are kept after it leaves. The ground rules' liquidity floor still applies.
Universes: sp900 (both indexes, the headline), and each index alone.

Run:    PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_sp900.py
Report: PYTHONPATH="src;research" .venv/Scripts/python.exe research/run_wtt_sp900.py --report
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date

import numpy as np

import benchmarks
import panel as P
import report_rules as rr
import run_wtt as W
from engine import OUTPUT, PERIODS, PERIOD_TITLES, metrics, run_test, simulate, spy_series, yearly
from strategies.weekend_trend import WeekendTrend, week_ends

OUT = OUTPUT / "wtt900"
MEMBERS = P.CACHE / f"wtt_members_{W.THROUGH}.npz"
UNIVERSES = {"sp900": "S&P 500 + MidCap 400", "sp500": "S&P 500", "sp400": "S&P MidCap 400"}
PREFIX = {"sp900": "wtt900", "sp500": "wtt500", "sp400": "wtt400"}
SEEDS = 200
f, table, pct = W.f, W.table, W.pct


def load_members(p) -> dict:
    if not MEMBERS.exists():
        import wtt_universe
        m = wtt_universe.membership(p)
        np.savez_compressed(MEMBERS, sp500=m["sp500"], sp400=m["sp400"],
                            info=np.array(json.dumps({"agreement": m["agreement"], "unmatched": m["unmatched"]})))
    data = np.load(MEMBERS)
    return {"sp500": data["sp500"], "sp400": data["sp400"], "info": json.loads(str(data["info"]))}


_W: dict = {}


def _init() -> None:
    p, b = P.load(through=W.THROUGH), benchmarks.load()
    m = load_members(p)
    WeekendTrend.members = {"sp500": m["sp500"], "sp400": m["sp400"]}
    _W.update(panel=p, bench=b, spy={k: spy_series(p, b["spy_dividends"], s, e or str(p.dates[-1]))
                                     for k, (s, e) in PERIODS.items()})


def mc_task(task: tuple) -> dict:
    universe, rule, period, seed = task
    p, b = _W["panel"], _W["bench"]
    start, end = PERIODS[period]
    s = WeekendTrend()
    s.setup(p, W.params_for(rule, universe, "random", seed))
    run = simulate(p, s, start, end, W.RULES)
    m = metrics(run, _W["spy"][period], b["tbill3m"])
    row = {"universe": universe, "rule": rule, "period": period, "seed": seed,
           **{k: float(m[k]) for k in W.MC_KEYS}, "signals": m["counts"]["signals"],
           "no_slot": m["counts"]["skipped_no_slot"], "yearly": m["yearly"]}
    if period == "combined":
        row["equity"] = run["equity"].astype(np.float32)
    return row


def buy_and_hold(p, symbol: str) -> dict:
    """Price-only buy-and-hold of an ETF in the panel, per period (no dividend history is cached for it)."""
    j = p.index[symbol]
    out = {}
    for period, (start, end) in PERIODS.items():
        a, z = p.session_index(start), int(np.searchsorted(p.dates, end or str(p.dates[-1]), side="right")) - 1
        c = p.close_ff[a:z + 1, j]
        years = (date.fromisoformat(str(p.dates[z])) - date.fromisoformat(str(p.dates[a]))).days / 365.25
        out[period] = {"cagr_price": 100 * ((c[-1] / c[0]) ** (1 / years) - 1),
                       "max_drawdown": 100 * float((1 - c / np.maximum.accumulate(c)).max())}
    return out


def universe_stats(p, members: dict) -> dict:
    """Weekly member counts in the panel, and how many cleared the liquidity floor."""
    ends = np.flatnonzero(week_ends(p.dates))
    ends = ends[p.dates[ends] >= "2016-01-01"]
    stock = (p.kind == "stock")[None, :]
    out = {}
    for u in UNIVERSES:
        m = members["sp500"] | members["sp400"] if u == "sp900" else members[u]
        n = m[ends].sum(axis=1)
        liquid = (m & p.eligible & stock)[ends].sum(axis=1)
        out[u] = {"members_median": int(np.median(n)), "members_min": int(n.min()), "members_max": int(n.max()),
                  "liquid_median": int(np.median(liquid)), "liquid_share": 100 * float(liquid.sum() / n.sum())}
    us = W.WeekendTrend()
    us.setup(p, W.params_for("ratchet", "us", "ret63"))
    n = us.eligible_mask[ends].sum(axis=1)
    out["us"] = {"liquid_median": int(np.median(n))}
    return out


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    W._init()     # foreign companies, for the all-stocks comparison runs in this process
    _init()
    p, b, spy = _W["panel"], _W["bench"], _W["spy"]
    members = load_members(p)
    tasks = [(u, rule, period, seed) for u in UNIVERSES for rule in W.RULE_NAMES for period in PERIODS
             for seed in range(SEEDS)]
    with ProcessPoolExecutor(W.WORKERS, initializer=_init) as pool:
        rows = list(pool.map(mc_task, tasks, chunksize=8))
    print(f"Monte Carlo: {len(rows)} runs, {time.time() - t0:.0f} s")
    with (OUT / "montecarlo.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["universe", "rule", "period", "seed", *W.MC_KEYS, "signals", "skipped_no_slot"])
        for r in rows:
            w.writerow([r["universe"], r["rule"], r["period"], r["seed"], *(round(r[k], 4) for k in W.MC_KEYS),
                        r["signals"], r["no_slot"]])

    base = json.loads((W.OUT / "results.json").read_text(encoding="utf-8"))
    spy_stats, spy_yearly = base["spy"], base["spy_yearly"]
    mc, paired, median_seed = {}, {}, {}
    for u in UNIVERSES:
        for rule in W.RULE_NAMES:
            for period in PERIODS:
                sub = [r for r in rows if r["universe"] == u and r["rule"] == rule and r["period"] == period]
                c = np.array([r["cagr"] for r in sub])
                mc[f"{u}/{rule}/{period}"] = {
                    **{f"{k}_p{q}": pct([r[k] for r in sub], q) for k in ("cagr", "max_drawdown", "sharpe")
                       for q in (5, 50, 95)},
                    **{f"{k}_median": pct([r[k] for r in sub], 50) for k in W.MC_KEYS},
                    "beat_spy_total": 100 * float((c > spy_stats[period]["cagr"]).mean()),
                    "beat_spy_price": 100 * float((c > spy_stats[period]["cagr_price"]).mean()),
                    "positive": 100 * float((c > 0).mean()),
                    "yearly_median": {y: pct([r["yearly"][y] for r in sub], 50) for y in sub[0]["yearly"]},
                    "yearly_p5": {y: pct([r["yearly"][y] for r in sub], 5) for y in sub[0]["yearly"]},
                    "yearly_p95": {y: pct([r["yearly"][y] for r in sub], 95) for y in sub[0]["yearly"]},
                }
            sub = sorted((r for r in rows if r["universe"] == u and r["rule"] == rule and r["period"] == "combined"),
                         key=lambda r: r["cagr"])
            median_seed[f"{u}/{rule}"] = sub[len(sub) // 2]["seed"]
        for period in PERIODS:
            a = {r["seed"]: r["cagr"] for r in rows if r["universe"] == u and r["rule"] == "ratchet" and r["period"] == period}
            z = {r["seed"]: r["cagr"] for r in rows if r["universe"] == u and r["rule"] == "recompute" and r["period"] == period}
            d = np.array([z[k] - a[k] for k in a])
            paired[f"{u}/{period}"] = {"median_diff": pct(d, 50), "p5": pct(d, 5), "p95": pct(d, 95),
                                       "recompute_better": 100 * float((d > 0).mean())}

    tests = {}
    for u in UNIVERSES:
        for rule in W.RULE_NAMES:
            tests[f"{PREFIX[u]}_{rule}_random_median"] = (u, rule, "random", median_seed[f"{u}/{rule}"])
    for rule in W.RULE_NAMES:
        for rank in ("ret63", "roc"):
            tests[f"wtt900_{rule}_{rank}"] = ("sp900", rule, rank, 0)
    fixed = {}
    for name, (u, rule, rank, seed) in tests.items():
        params = W.params_for(rule, u, rank, seed)
        desc = (f"Weekend Trend Trader on {UNIVERSES[u]} members (point in time). {W.RULE_NAMES[rule]}. Ranking: "
                + (f"random, seed {seed} (the median of {SEEDS} random runs on the combined period)" if rank == "random"
                   else W.RANK_NAMES[rank]) + ". 5% of equity per position, at most 20.")
        summary = run_test(p, name, WeekendTrend, params, b, rules=W.RULES, description=desc)
        fixed[name] = {"universe": u, "rule": rule, "rank": rank, "seed": seed,
                       "periods": {k: {m: float(v[m]) for m in W.MC_KEYS} for k, v in summary["results"].items()},
                       **{k: v for k, v in W.detail(p, b, spy, params).items() if k != "equity"}}
        print(name, {k: round(v["cagr"], 1) for k, v in fixed[name]["periods"].items()})

    start = PERIODS["combined"][0]
    dates = p.dates[p.session_index(start):]
    cols = {"spy_total": W.RULES.capital * spy["combined"]["total"],
            "spy_price": W.RULES.capital * spy["combined"]["price"]}
    j = p.index["IJH"]
    ijh = p.close_ff[p.session_index(start):, j]
    cols["ijh_price"] = W.RULES.capital * ijh / ijh[0]
    for rule in W.RULE_NAMES:
        eq = np.array([r["equity"] for r in rows if r["universe"] == "sp900" and r["rule"] == rule
                       and r["period"] == "combined"], dtype=float)
        dd = 1 - eq / np.maximum.accumulate(eq, axis=1)
        for q in (5, 50, 95):
            cols[f"{rule}_p{q}"] = np.percentile(eq, q, axis=0)
            cols[f"{rule}_dd_p{q}"] = -100 * np.percentile(dd, 100 - q, axis=0)
        s = WeekendTrend()
        s.setup(p, W.params_for(rule, "sp900", "ret63"))
        cols[f"{rule}_ret63"] = simulate(p, s, start, None, W.RULES)["equity"]
    with (OUT / "curves_combined.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", *cols])
        for i, d in enumerate(dates):
            w.writerow([d, *(round(float(v[i]), 2) for v in cols.values())])

    results = {"data_through": str(p.dates[-1]), "seeds": SEEDS, "rules": W.RULES.__dict__, "spy": spy_stats,
               "spy_yearly": spy_yearly, "ijh": buy_and_hold(p, "IJH"), "mc": mc, "paired": paired,
               "median_seed": median_seed, "fixed": fixed, "universe": universe_stats(p, members),
               "membership": members["info"], "all_us": {k: base["mc"][k] for k in base["mc"]},
               "all_us_fixed": {k: base["fixed"][k]["periods"] for k in ("wtt_ratchet_ret63", "wtt_recompute_ret63",
                                                                           "wtt_ratchet_roc", "wtt_recompute_roc")}}
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    (OUT / "report.md").write_text(report(results), encoding="utf-8")
    print(f"done in {time.time() - t0:.0f} s")


# ----------------------------------------------------------------------
# Report


PERIOD_ORDER = ["combined", "in_sample", "out_of_sample"]


def findings(res: dict) -> str:
    mc, spy, fx, paired, uni, ijh = res["mc"], res["spy"], res["fixed"], res["paired"], res["universe"], res["ijh"]
    us = res["all_us"]
    a, z = mc["sp900/ratchet/combined"], mc["sp900/recompute/combined"]
    ai, ao = mc["sp900/ratchet/in_sample"], mc["sp900/ratchet/out_of_sample"]
    ua, ui, uo = us["ratchet/combined"], us["ratchet/in_sample"], us["ratchet/out_of_sample"]
    y, uy = a["yearly_median"], ua["yearly_median"]
    med = fx["wtt900_ratchet_random_median"]["trades"]
    s5r, s5z = mc["sp500/ratchet/combined"], mc["sp500/recompute/combined"]
    s5zo = mc["sp500/recompute/out_of_sample"]
    s4r, s4z = mc["sp400/ratchet/combined"], mc["sp400/recompute/combined"]
    s4ro, s4zo = mc["sp400/ratchet/out_of_sample"], mc["sp400/recompute/out_of_sample"]
    ret63, roc = fx["wtt900_ratchet_ret63"]["periods"], fx["wtt900_ratchet_roc"]["periods"]
    ret63_dd = fx["wtt900_ratchet_ret63"]["metrics"]["max_drawdown"]
    roc_dd = fx["wtt900_ratchet_roc"]["metrics"]["max_drawdown"]
    usf = res["all_us_fixed"]
    base = json.loads((W.OUT / "results.json").read_text(encoding="utf-8"))
    filt = base["index_filter"]["combined"]
    up_weeks = filt["weeks"] * (1 - filt["down_pct"] / 100)
    us_signals = base["fixed"]["wtt_ratchet_random_median"]["trades"]["signals"] / up_weeks
    items = [
        f"**Index stocks only: a lower return and a smaller drawdown, still far behind SPY.** On S&P 500 and MidCap "
        f"400 members, the median of {res['seeds']} random selections made {f(a['cagr_p50'])}%/yr (5th–95th percentile "
        f"{f(a['cagr_p5'])}–{f(a['cagr_p95'])}%) with a {f(a['max_drawdown_p50'], 0)}% maximum drawdown, against "
        f"{f(ua['cagr_p50'])}% and {f(ua['max_drawdown_p50'], 0)}% on all liquid US stocks. With the recomputed stop: "
        f"{f(z['cagr_p50'])}% and {f(z['max_drawdown_p50'], 0)}%. SPY made {f(spy['combined']['cagr'])}%/yr with "
        f"dividends ({f(spy['combined']['cagr_price'])}% on price), and no run beat either figure.",
        f"**It swapped the speculative winners and losers for steadier stocks.** In 2016–2021 the median fell from "
        f"{f(ui['cagr_p50'])}% to {f(ai['cagr_p50'])}%/yr: 2020 made {f(y['2020'], 0, '%', True)} instead of "
        f"{f(uy['2020'], 0, '%', True)}, though 2021 made {f(y['2021'], 0, '%', True)} instead of "
        f"{f(uy['2021'], 0, '%', True)}. From 2022 it rose from {f(uo['cagr_p50'])}% to {f(ao['cagr_p50'])}%/yr: 2022 "
        f"{f(y['2022'], 0, '%', True)} instead of {f(uy['2022'], 0, '%', True)}, 2025 {f(y['2025'], 0, '%', True)} "
        f"instead of {f(uy['2025'], 0, '%', True)}, 2026 so far {f(y['2026'], 0, '%', True)} instead of "
        f"{f(uy['2026'], 0, '%', True)}. Average win {f(a['avg_win_pct_median'], 0, '%', True)} and average loss "
        f"{f(a['avg_loss_pct_median'], 0, '%', True)}, against {f(ua['avg_win_pct_median'], 0, '%', True)} and "
        f"{f(ua['avg_loss_pct_median'], 0, '%', True)} on all stocks.",
        f"**Inside the indexes, ranking by momentum helps instead of ruining it.** Ranked by 63-day return the system "
        f"made {f(ret63['combined']['cagr'])}%/yr ({f(ret63_dd, 0)}% drawdown), and by 20-week rate of change "
        f"{f(roc['combined']['cagr'])}% ({f(roc_dd, 0)}%), both above the random runs' 95th percentile of "
        f"{f(a['cagr_p95'])}%. On all stocks the same rankings lost {f(-usf['wtt_ratchet_ret63']['combined']['cagr'])}% "
        f"and {f(-usf['wtt_ratchet_roc']['combined']['cagr'])}% a year. The momentum rankings' losses came from "
        "speculative stocks outside the indexes; within them, the strongest stocks were worth picking. Even so, "
        f"the best ranking trailed SPY by {f(spy['combined']['cagr_price'] - ret63['combined']['cagr'])} points a "
        f"year on price ({f(spy['combined']['cagr'] - ret63['combined']['cagr'])} with dividends), with a deeper "
        "drawdown than the random runs.",
        f"**The S&P 500 alone was steadier; the MidCap 400 alone was weak.** S&P 500 members: "
        f"{f(s5r['cagr_p50'])}%/yr with the stop as described and {f(s5z['cagr_p50'])}% recomputed, with "
        f"{f(min(s5r['max_drawdown_p50'], s5z['max_drawdown_p50']), 0)}–"
        f"{f(max(s5r['max_drawdown_p50'], s5z['max_drawdown_p50']), 0)}% drawdowns. From 2022 the recomputed "
        f"version made {f(s5zo['cagr_p50'])}%/yr, close to SPY's {f(spy['out_of_sample']['cagr_price'])}% price "
        f"return ({f(s5zo['beat_spy_price'], 0)}% of runs beat it). MidCap 400 members: {f(s4r['cagr_p50'])}% and "
        f"{f(s4z['cagr_p50'])}%, and {f(s4ro['cagr_p50'], 1, '%', True)} / {f(s4zo['cagr_p50'], 1, '%', True)} a year "
        f"from 2022, against {f(ijh['combined']['cagr_price'])}% and {f(ijh['out_of_sample']['cagr_price'])}% for "
        "IJH on price alone.",
        f"**The stop rule still has no consistent winner.** Paired by seed, the recomputed stop did better in "
        f"{f(paired['sp500/combined']['recompute_better'], 0)}% of S&P 500 runs "
        f"({f(paired['sp500/combined']['median_diff'], 1, sign=True)} pts/yr), "
        f"{f(paired['sp400/combined']['recompute_better'], 0)}% of MidCap 400 runs "
        f"({f(paired['sp400/combined']['median_diff'], 1, sign=True)}) and {f(paired['sp900/combined']['recompute_better'], 0)}% "
        f"of combined runs ({f(paired['sp900/combined']['median_diff'], 1, sign=True)}). Index stocks rarely fall 40% "
        f"between down weeks: in the median S&P 900 run, {med['exit_10']} of {med['trades']} exits were at the 10% "
        f"stop and {med['exit_40']} at the 40% stop.",
        f"**A smaller field.** About {uni['sp900']['members_median']} stocks were members in a typical week and "
        f"{uni['sp900']['liquid_median']} cleared the liquidity floor, against {uni['us']['liquid_median']:,} liquid "
        f"US stocks in the first study. The floor kept {f(uni['sp500']['liquid_share'], 1)}% of S&P 500 member-weeks "
        f"but only {f(uni['sp400']['liquid_share'], 0)}% of MidCap 400 ones. About "
        f"{med['signals'] / up_weeks:.0f} stocks met the entry rules in a week with the filter up, against "
        f"{us_signals:.0f} in the first study, so selection luck matters less: the random runs' 5th–95th percentile range is "
        f"{f(a['cagr_p95'] - a['cagr_p5'], 1)} points wide, against {f(ua['cagr_p95'] - ua['cagr_p5'], 1)}.",
    ]
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))


def rules_section(seeds: int) -> list[str]:
    return rr.section(
        ["Nick Radge's Weekend Trend Trader (*Weekend Trend Trader*, 2012), as in the all-stocks study, checked on "
         "weekly closes. A week's close is the close of its last session; orders go in at the next session's open.", "",
         "- Index filter: SPY's weekly close above the mean of its last 10 weekly closes. SPY stands in for both "
         "indexes.",
         "- Entry: the stock's weekly close is at least the highest of its previous 20 weekly closes, and its 20-week "
         "rate of change is at least 30%. No new entries while the index filter is down.",
         "- Stop: 40% below the highest weekly close since the signal week while the index filter is up, 10% below "
         "once it is down. A weekly close under the stop sells at the next open; there are no intraday stops.",
         "  - *Stop as described* (the book): the stop never moves down. After a down week it stays at the 10% level "
         "until 40% below a new high passes it.",
         "  - *Recomputed stop*: recomputed each week from the highest close with that week's percentage, so it "
         "loosens back to 40% when the index recovers.",
         "- Size: 5% of equity at the previous close per position, at most 20 positions, no margin. An entry that "
         "needs more than the cash left is shrunk to it; with no cash left the signal is skipped."],
        ["Choices where the book is silent:", "",
         f"- Selection: the book gives no rule for more signals than free slots. Random order is the headline, run "
         f"{seeds} times per universe, stop rule and period; the 63-session return and the 20-week rate of change, "
         "highest first, are fixed alternatives.",
         "- Universe: members of the S&P 500 or S&P MidCap 400 at the signal week (see *Membership and method*), "
         "within the liquidity floor: an as-traded close of $5 or more and 20-day average dollar volume "
         "of $20M or more at the signal close.",
         "- Costs: slippage 0.10% a side, 0.25% under $20 as traded. Trade returns exclude dividends; idle cash earns "
         "nothing.",
         "- Periods: in-sample 2016–2021, out-of-sample 2022 onward, and combined, each a separate run from $100,000 "
         "that closes open positions at its end. Nothing was tuned, so the split is only a robustness check.",
         "- Benchmarks: SPY with dividends and on price alone, and IJH (the iShares S&P MidCap 400 ETF) on price alone. "
         "Trade returns exclude dividends, so the price returns are the like-for-like comparison.",
         "- Delisted stocks are thin before 2021, so 2016–2020 is flattered by survivorship."])


def report(res: dict) -> str:
    mc, spy, fx, paired, ijh, us = res["mc"], res["spy"], res["fixed"], res["paired"], res["ijh"], res["all_us"]
    n = res["seeds"]
    title = {p: PERIOD_TITLES[p] for p in PERIOD_ORDER}
    out = ["# Weekend Trend Trader on the S&P 500 and MidCap 400", "",
           f"Data through {res['data_through']}. The rules, stop variants, sizing and costs are those of the "
           "all-stocks study, [*Nick Radge's Weekend Trend Trader: two stop rules*](/reports?report=weekend-trend-trader); only the universe changes. A stock can be bought only "
           "while it is in the S&P 500 or S&P MidCap 400 at the signal week's close, and is kept after it leaves.", "",
           *rules_section(n),
           "## Findings", "", findings(res), "", "![Growth of $100,000 and drawdowns](equity_drawdown.png)", ""]

    out += ["## Random selection by universe", "",
            f"{n} runs per universe, stop rule and period; 5th / median / 95th percentile across runs.", ""]
    rows = []
    for period in PERIOD_ORDER:
        for u in ("sp900", "sp500", "sp400"):
            for rule in W.RULE_NAMES:
                m = mc[f"{u}/{rule}/{period}"]
                rows.append([title[period], UNIVERSES[u], W.RULE_NAMES[rule],
                             f"{f(m['cagr_p5'])}% / **{f(m['cagr_p50'])}%** / {f(m['cagr_p95'])}%",
                             f"{f(m['max_drawdown_p50'], 0)}%", f(m["sharpe_p50"], 2),
                             f"{f(m['beat_spy_total'], 0)}% / {f(m['beat_spy_price'], 0)}%"])
        for rule in W.RULE_NAMES:
            m = us[f"{rule}/{period}"]
            rows.append([title[period], "All liquid US stocks (first study)", W.RULE_NAMES[rule],
                         f"{f(m['cagr_p5'])}% / **{f(m['cagr_p50'])}%** / {f(m['cagr_p95'])}%",
                         f"{f(m['max_drawdown_p50'], 0)}%", f(m["sharpe_p50"], 2),
                         f"{f(m['beat_spy_total'], 0)}% / {f(m['beat_spy_price'], 0)}%"])
        rows.append([title[period], "SPY buy-and-hold", "",
                     f"**{f(spy[period]['cagr'])}%** with dividends, {f(spy[period]['cagr_price'])}% price",
                     f"{f(spy[period]['max_drawdown'], 0)}%", "", ""])
        rows.append([title[period], "IJH buy-and-hold (S&P MidCap 400)", "",
                     f"{f(ijh[period]['cagr_price'])}% price only", f"{f(ijh[period]['max_drawdown'], 0)}%", "", ""])
    out += [table(["Period", "Universe", "Stop", "CAGR", "Max drawdown (median)", "Sharpe (median)",
                   "Runs beating SPY: with dividends / price only"], rows), "",
            "Trade returns exclude dividends and idle cash earns nothing, so SPY's price return is the like-for-like "
            "figure. IJH is shown on price alone (no dividend history was available for it).", ""]

    out += ["## Ranking", "", "Combined period. Fixed rankings are single deterministic runs; random is the median run.", ""]
    usf = res["all_us_fixed"]
    rows = []
    for label, key900, keyus in (("Random (median)", None, None), ("63-session return, highest first", "ret63", "ret63"),
                                 ("20-week rate of change, highest first", "roc", "roc")):
        cells = [label]
        for rule in W.RULE_NAMES:
            if key900 is None:
                m9, mu = mc[f"sp900/{rule}/combined"], us[f"{rule}/combined"]
                cells += [f"{f(m9['cagr_p50'])}% / {f(m9['max_drawdown_p50'], 0)}%",
                          f"{f(mu['cagr_p50'])}% / {f(mu['max_drawdown_p50'], 0)}%"]
            else:
                m9 = fx[f"wtt900_{rule}_{key900}"]
                cells += [f"{f(m9['periods']['combined']['cagr'])}% / {f(m9['periods']['combined']['max_drawdown'], 0)}%",
                          f"{f(usf[f'wtt_{rule}_{keyus}']['combined']['cagr'])}% / "
                          f"{f(usf[f'wtt_{rule}_{keyus}']['combined']['max_drawdown'], 0)}%"]
        rows.append(cells)
    out += [table(["Ranking: CAGR / max drawdown", "Stop as described: S&P 900", "Stop as described: all stocks",
                   "Recomputed: S&P 900", "Recomputed: all stocks"], rows), ""]
    rows = [[W.RANK_NAMES[rank], W.RULE_NAMES[rule]] +
            [f"{f(fx[f'wtt900_{rule}_{rank}']['periods'][p]['cagr'])}% / "
             f"{f(fx[f'wtt900_{rule}_{rank}']['periods'][p]['max_drawdown'], 0)}%" for p in ("in_sample", "out_of_sample")]
            for rank in ("ret63", "roc") for rule in W.RULE_NAMES]
    out += ["S&P 900 fixed rankings by period (CAGR / max drawdown):", "",
            table(["Ranking", "Stop", title["in_sample"], title["out_of_sample"]], rows), ""]

    out += ["## The two stop rules", "", "CAGR of the recomputed stop minus the stop as described, paired by seed:", ""]
    out += [table(["Universe", "Period", "Median difference", "5th–95th percentile", "Recomputed did better"],
                  [[UNIVERSES[u], title[p], f(paired[f'{u}/{p}']['median_diff'], 2, ' pts', True),
                    f"{f(paired[f'{u}/{p}']['p5'], 1, sign=True)} to {f(paired[f'{u}/{p}']['p95'], 1, sign=True)}",
                    f"{f(paired[f'{u}/{p}']['recompute_better'], 0)}% of runs"]
                   for u in ("sp900", "sp500", "sp400") for p in PERIOD_ORDER]), ""]

    names = ["wtt900_ratchet_random_median", "wtt900_recompute_random_median", "wtt900_ratchet_ret63"]
    heads = ["Stop as described, median random run", "Recomputed stop, median random run",
             "Stop as described, 63-day ranking"]
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
        trow("Beta / correlation to SPY (daily)", lambda x: f"{f(c(x)['beta'], 2)} / {f(c(x)['corr_spy'], 2)}"),
        trow("Average invested / positions held", lambda x: f"{f(m(x)['exposure_avg_invested_pct'], 0)}% / {f(t(x)['avg_held'], 1)}"),
        trow("Trades / per year", lambda x: f"{t(x)['trades']:,} / {f(t(x)['per_year'], 0)}"),
        trow("Win rate", lambda x: f"{f(t(x)['win_rate'])}%"),
        trow("Average win / average loss", lambda x: f"{f(t(x)['avg_win_pct'], 1, '%', True)} / {f(t(x)['avg_loss_pct'], 1, '%', True)}"),
        trow("Payoff ratio", lambda x: f(t(x)["payoff"], 2)),
        trow("Average / median trade", lambda x: f"{f(t(x)['expectancy_pct'], 1, '%', True)} / {f(t(x)['median_pct'], 1, '%', True)}"),
        trow("Profit factor", lambda x: f(t(x)["profit_factor"], 2)),
        trow("Average holding: winners / losers", lambda x: f"{f(t(x)['hold_win_weeks'], 0)} / {f(t(x)['hold_loss_weeks'], 0)} weeks"),
        trow("Losses worse than -40%", lambda x: f"{t(x)['losses_over_40']}"),
        trow("Top 10% of trades, share of profit", lambda x: f(t(x)["top10_share"], 0, "%")),
        trow("Exits: 40% stop / 10% stop / delisted / end of test",
             lambda x: f"{t(x)['exit_40']} / {t(x)['exit_10']} / {t(x)['exit_delisted']} / {t(x)['exit_end']}"),
        trow("Signals / entries / no free slot / no cash",
             lambda x: f"{t(x)['signals']:,} / {t(x)['entries']:,} / {t(x)['no_slot']:,} / {t(x)['no_cash']:,}"),
    ]
    out += ["## Trade detail (S&P 900, combined period)", "", table(["", *heads], rows), ""]

    years = list(mc["sp900/ratchet/combined"]["yearly_median"])
    rows = [[yr] + [f"{f(mc[f'{u}/ratchet/combined']['yearly_median'][yr])}%" for u in ("sp900", "sp500", "sp400")]
            + [f"{f(us['ratchet/combined']['yearly_median'][yr])}%", f"{f(res['spy_yearly'][yr])}%"] for yr in years]
    out += ["## Yearly returns", "", "Median random run, stop as described.", "",
            table(["Year", "S&P 900", "S&P 500", "S&P MidCap 400", "All liquid US stocks", "SPY with dividends"], rows), ""]
    out += [membership_section(res)]
    return "\n".join(out) + "\n"


def membership_section(res: dict) -> str:
    agree = res["membership"]["agreement"]
    uni = res["universe"]

    def span(lo, hi):
        rows = [a for a in agree if lo <= a["date"] < hi]
        if not rows:
            return "–"
        pcts = [100 * a["both"] / a["ijh"] for a in rows]
        return f"{min(pcts):.0f}–{max(pcts):.0f}% ({len(rows)} dates)"

    unmatched = res["membership"]["unmatched"]
    top = sorted(((k, v) for k, v in unmatched.items() if not k.startswith("ijh:")), key=lambda kv: -kv[1]["weeks"])
    return "\n".join([
        "## Membership and method", "",
        "- **S&P 500:** the data provider's dated change log, as resolved for the S&P 500 GARP study (renames and price series "
        "checked there).",
        "- **S&P MidCap 400:** the data provider has no history for it. Each week uses the constituent table of Wikipedia's \"List of S&P 400 "
        "companies\" as it stood at the week's close, together with the latest holdings of IJH (iShares Core S&P "
        "Mid-Cap ETF, which holds the index) saved by the Wayback Machine in the previous 190 days. "
        "Share of IJH's holdings that the Wikipedia list in force also had:",
        f"  - 2015–2016: {span('2015', '2017')}. The page was stale until a December 2016 rewrite, missing long-time "
        "members such as Cracker Barrel and Casey's.",
        f"  - 2017–2018: {span('2017', '2019')}; 2019: {span('2019', '2020')}; 2020–Jan 2021: {span('2020', '2022')}.",
        f"  - 2023 onward: {span('2023', '2027')}. No IJH snapshot was saved between January 2021 and March 2023.",
        "  - Adding IJH's names fills what a stale list missed. The names this keeps a little too long are ones that "
        "had just left the index, which a rule buying 20-week highs rarely touches.",
        f"- **Coverage:** a typical week had {uni['sp500']['members_median']} S&P 500 and {uni['sp400']['members_median']} "
        "MidCap 400 members with prices in the panel. Missing: companies acquired before 2021 with no price history in the data "
        "(Eaton Vance, CoreLogic, Dunkin', Tech Data and others), second share classes (GOOGL, FOX, NWS; the "
        "other class is in), and REITs and trusts, which the stock universe here leaves out. Renamed tickers are matched by "
        "a hand-checked list (CREE→WOLF, MLHR→MLKN, GPS→GAP, ERI→CZR and others); a ticker whose current owner is "
        "another company (RBC, Regal Beloit until 2021) is left out.",
        f"- **Liquidity floor:** unchanged ($5 as traded, $20M of 20-day average dollar volume at the signal). It kept "
        f"{f(uni['sp500']['liquid_share'], 1)}% of S&P 500 member-weeks and {f(uni['sp400']['liquid_share'], 0)}% "
        "of MidCap 400 ones.",
        "- **Index filter:** SPY above its 10-week average, as before, for both indexes.",
        "- Everything else (sizing, stops, costs, periods, survivorship caveats) as in the all-stocks study. The "
        "largest gaps in membership coverage: " + ", ".join(f"{k.split(':')[1]} ({v['weeks']} weeks)"
                                                          for k, v in top[:8]) + ".",
        "",
        "Files: `montecarlo.csv`, `curves_combined.csv`, `results.json`; trade lists for each universe's median random "
        "run and the fixed rankings are under *Compare variations*.",
    ])


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
        (OUT / "report.md").write_text(report(res), encoding="utf-8")
    else:
        main()
