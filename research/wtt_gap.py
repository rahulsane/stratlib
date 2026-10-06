"""Why the published 18% differs from this harness's Weekend Trend Trader results (output/wtt/gap.md).

QuantifiedStrategies' figures come from their own rules, "loosely based on" WTT (not from the book), on
Norgate data from 1990 (survivorship-free), without commissions or slippage, with 10 positions of 10%, and with
average holding periods of 65-121 weeks. This script changes one difference at a time on 2016-2026 data
(combined period, random selection, SEEDS runs each), then all together:
  costs    no slippage
  ten      10 positions of 10% instead of 20 of 5%
  wide     a 40% trailing stop that never tightens (the index filter only gates entries)
  all      the three together
It also measures how often the 10-week index filter turned down in 1990-2015 and 2016-2026, on the S&P 500
series of the Traveling Trader tests (tt_data.load_spx).

Run: PYTHONPATH="src;research" .venv/Scripts/python.exe research/wtt_gap.py
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import numpy as np

import run_wtt as W
import run_wtt_sp900 as S
from engine import PERIODS, metrics, simulate
from strategies.weekend_trend import WeekendTrend

SEEDS = 100
OUT = W.OUT / "gap.md"
CHANGES = {
    "base": "WTT as described (stop never moves down), 20 x 5%, slippage",
    "costs": "... without slippage",
    "ten": "... with 10 positions of 10%",
    "wide": "... with a 40% stop that never tightens",
    "all": "all three changes together",
}
UNIVERSE_NAMES = {"us": "All liquid US stocks", "sp500": "S&P 500", "sp400": "S&P MidCap 400"}


def setup_for(change: str) -> tuple[dict, object]:
    params, rules = {"stop_rule": "ratchet"}, W.RULES
    if change in ("costs", "all"):
        rules = replace(rules, slippage_pct=0.0, slippage_low_price_pct=0.0)
    if change in ("ten", "all"):
        rules = replace(rules, max_positions=10, max_position_pct=10.0)
        params["position_pct"] = 10.0
    if change in ("wide", "all"):
        params["stop_down_pct"] = 40.0
    return params, rules


def _init() -> None:
    W._init()
    S._init()


def task(job: tuple) -> dict:
    universe, change, rank, seed = job
    p, b = S._W["panel"], S._W["bench"]
    params, rules = setup_for(change)
    s = WeekendTrend()
    s.setup(p, {**params, "universe": universe, "rank": rank, "seed": seed})
    start, end = PERIODS["combined"]
    run = simulate(p, s, start, end, rules)
    m = metrics(run, S._W["spy"]["combined"], b["tbill3m"])
    weeks = [t.holding_days / 7 for t in run["trades"]]
    return {"universe": universe, "change": change, "rank": rank, "seed": seed,
            **{k: float(m[k]) for k in ("cagr", "max_drawdown", "win_rate", "avg_win_pct", "avg_loss_pct",
                                        "trades", "exposure_avg_invested_pct")},
            "weeks": float(np.mean(weeks)), "expectancy_pct": float(np.mean([t.return_pct for t in run["trades"]]))}


def filter_eras() -> dict:
    import tt_data
    spx = tt_data.load_spx()
    days, price = spx.days, spx.price
    week = (np.array(days, dtype="datetime64[D]").astype(np.int64) + 3) // 7
    ends = np.flatnonzero(np.r_[week[:-1] != week[1:], True])
    wd, wc = days[ends], price[ends]
    sma = np.convolve(wc, np.ones(10) / 10, mode="full")[:len(wc)]
    down = wc < sma
    down[:9] = False
    out = {}
    for name, (a, z) in {"1990–2015": ("1990-01-01", "2015-12-31"), "2016–2026": ("2016-01-01", "2026-09-30"),
                         "1990–1999": ("1990-01-01", "1999-12-31"), "2000–2009": ("2000-01-01", "2009-12-31"),
                         "2010–2015": ("2010-01-01", "2015-12-31")}.items():
        k = np.flatnonzero((wd >= a) & (wd <= z))
        d = down[k]
        spells = int(d[0] + np.sum(d[1:] & ~d[:-1]))
        years = len(k) / 52.18
        out[name] = {"down_pct": 100 * float(d.mean()), "spells_per_year": spells / years,
                     "index_cagr": 100 * float((wc[k[-1]] / wc[k[0]]) ** (1 / years) - 1)}
    return out


def main() -> None:
    _init()
    jobs = [(u, c, "random", seed) for u in UNIVERSE_NAMES for c in CHANGES for seed in range(SEEDS)]
    jobs += [(u, c, "ret63", 0) for u in UNIVERSE_NAMES for c in ("base", "all")]
    with ProcessPoolExecutor(W.WORKERS, initializer=_init) as pool:
        rows = list(pool.map(task, jobs, chunksize=8))
    summary = {}
    for u in UNIVERSE_NAMES:
        for c in CHANGES:
            sub = [r for r in rows if r["universe"] == u and r["change"] == c and r["rank"] == "random"]
            summary[f"{u}/{c}"] = {k: float(np.median([r[k] for r in sub])) for k in sub[0]
                                   if isinstance(sub[0][k], float)}
            summary[f"{u}/{c}"].update(cagr_p5=float(np.percentile([r["cagr"] for r in sub], 5)),
                                      cagr_p95=float(np.percentile([r["cagr"] for r in sub], 95)))
        for c in ("base", "all"):
            r = next(r for r in rows if r["universe"] == u and r["change"] == c and r["rank"] == "ret63")
            summary[f"{u}/{c}/ret63"] = r
    res = {"seeds": SEEDS, "summary": summary, "eras": filter_eras()}
    (W.OUT / "gap.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


def report(res: dict) -> str:
    f, table = W.f, W.table
    s, eras = res["summary"], res["eras"]
    rows = []
    for u, name in UNIVERSE_NAMES.items():
        for c, label in CHANGES.items():
            m = s[f"{u}/{c}"]
            rows.append([name, label, f"{f(m['cagr_p5'])}% / **{f(m['cagr'])}%** / {f(m['cagr_p95'])}%",
                         f"{f(m['max_drawdown'], 0)}%", f(m["weeks"], 0), f"{f(m['win_rate'], 0)}%",
                         f"{f(m['avg_win_pct'], 0, '%', True)} / {f(m['avg_loss_pct'], 0, '%', True)}"])
        for c in ("base", "all"):
            m = s[f"{u}/{c}/ret63"]
            rows.append([name, ("as described" if c == "base" else "all three changes") + ", ranked by 63-day return",
                         f"{f(m['cagr'])}%", f"{f(m['max_drawdown'], 0)}%", f(m["weeks"], 0), f"{f(m['win_rate'], 0)}%",
                         f"{f(m['avg_win_pct'], 0, '%', True)} / {f(m['avg_loss_pct'], 0, '%', True)}"])
    era_rows = [[k, f"{f(v['down_pct'], 0)}%", f(v["spells_per_year"], 1), f"{f(v['index_cagr'])}%"]
                for k, v in eras.items()]
    return "\n".join([
        "# Why the published 18% differs", "",
        "QuantifiedStrategies' headline figures for the Weekend Trend Trader come from their own rules, which they "
        "describe as their interpretation (\"We have not read Nick Radge's ebook!\") or as \"loosely based on\" it. Their "
        "rules are members-only. What they disclose:", "",
        "- Russell 3000: 18%/yr, 33% drawdown, 1,165 trades held **101 weeks** on average, 49% winners averaging "
        "+37%, losers -15% (Substack, September 2026).",
        "- Index constituents, January 1990 to 2025, Norgate data free of survivorship bias, **no commissions or "
        "slippage**, **10 positions of 10%**: S&P 100 14.9%, S&P 500 19.9%, Nasdaq 100 16.5%, MidCap 400 22.9%, "
        "SmallCap 600 9.1% (from 1995), Russell 2000 0.1% (81% drawdown); average holding 65–121 weeks.", "",
        "WTT as written held positions about 16 weeks in this harness. The table changes one difference at a time "
        f"on 2016–2026 data ({res['seeds']} random selections each; 5th / median / 95th percentile CAGR):", "",
        table(["Universe", "Change", "CAGR", "Max drawdown", "Weeks held", "Win rate", "Average win / loss"], rows), "",
        "How often the 10-week filter turned down, on the S&P 500 (price index):", "",
        table(["Period", "Weeks below the average", "Down spells a year", "Index CAGR (price)"], era_rows), "",
        "Reading:",
        "- Costs are worth about half a point a year; 10 positions of 10% mostly widen the spread between lucky "
        "and unlucky selections.",
        "- A 40% stop that never tightens reproduces their holding periods (90–140 weeks) and adds 1–3.5 points a "
        "year here, most on the indexes. Its average win and loss are larger than theirs (+37% / -15% on the Russell "
        "3000), so their exit is not this one either, but it must be far wider than WTT's.",
        "- With all three, 2016–2026 gives 5–7%/yr at the median and about 10% at best (S&P 500, momentum "
        "ranking), against their 18–23%. The rest of the gap is the period: their 35 years include stretches this "
        "test cannot see. Pre-2016 data free of survivorship bias is not available here, so that part is inferred, "
        "not measured.",
        "- The 2016–2026 market was not choppier for this filter: it turned down about as often as in 1990–2015. "
        "What differed is breadth: SPY made 15.1%/yr, equal-weight S&P 500 (RSP) about 11.9% and the MidCap 400 "
        "(IJH) 9.4% on price. A strategy that spreads money evenly over breakout stocks earns roughly what the "
        "typical stock earns, and the typical stock trailed the largest ones by 3–6 points a year.",
        "- Their own spread across indexes (0.1% to 22.9% from the same rules) shows how much a 10-stock "
        "portfolio with a fixed ranking depends on which stocks it happened to pick.",
    ]) + "\n"


if __name__ == "__main__":
    if "--report" in sys.argv:
        res = json.loads((W.OUT / "gap.json").read_text(encoding="utf-8"))
        OUT.write_text(report(res), encoding="utf-8")
    else:
        main()
        OUT.write_text(report(json.loads((W.OUT / "gap.json").read_text(encoding="utf-8"))), encoding="utf-8")
