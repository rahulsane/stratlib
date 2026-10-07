"""Two Turtle variations requested after the first report (output/turtle/report.md):

1. Long and short, rules as written, on QQQ, GLD, SLV and USO (SPY and TLT dropped).
2. System 2 long only, on all six ETFs.

Each runs at the first report's three leverage settings (as written, gross capped at 2x and at 1x equity) over
the same periods, with the original six-ETF long/short runs alongside. Both changes remove what lost money over
the first report's full period, so they were chosen with hindsight; the report shows what the 2006-2015 data
alone said, and how the changes did from 2016.

Run: PYTHONPATH="src;research" .venv/Scripts/python research/run_turtle_variations.py
Output: research/output/turtle_variations/ (report.md, results.json, curves_full.csv, trades/)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

import report_rules as rr
import run_turtle as rt
import turtle_data as td
import turtle_sim as ts

OUT = Path(__file__).resolve().parent / "output" / "turtle_variations"
SUBSET = ("QQQ", "GLD", "SLV", "USO")
SETS = {
    "orig": ("All six, long and short (first report)", td.SYMBOLS, {}, ("S1", "S2", "S1+S2")),
    "v1": ("Without SPY and TLT, long and short", SUBSET, {}, ("S1", "S2", "S1+S2")),
    "v2": ("System 2 long only, all six", td.SYMBOLS, {"long_only": True}, ("S2",)),
}
SHORT = {"orig": "All six L/S", "v1": "No SPY/TLT L/S", "v2": "Long-only"}
SMALLER = (("v1", "S2"), ("v1", "S1+S2"), ("v2", "S2"), ("orig", "S2"))
RISKS = (0.01, 0.005, 0.0025)


def vkey(s: str, system: str, lev: str) -> str:
    return f"{s} {system} {lev}"


def vname(s: str, system: str, lev: str) -> str:
    sys_name = rt.SYSTEM_NAMES[system] if s != "v2" else "System 2"
    return f"{SETS[s][0]}: {sys_name}, {lev}"


def variants(sets=("orig", "v1", "v2")):
    return [(s, system, lev) for s in sets for lev in rt.LEVERAGE for system in SETS[s][3]]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    full = td.load()
    sub = td.load(SUBSET)
    assert np.array_equal(sub.days, full.days), "the subset must trade on the same sessions"
    ind_full, ind_sub = ts.indicators(full), ts.indicators(sub)
    datas = {"orig": (full, ind_full), "v1": (sub, ind_sub), "v2": (full, ind_full)}
    res: dict = {"data_through": str(full.days[-1]), "runs": {}}
    runs: dict = {}
    for period in rt.PERIODS:
        for s, system, lev in variants():
            data, ind = datas[s]
            run = rt.run_variant(data, ind, system, lev, period, **SETS[s][2])
            runs[(period, s, system, lev)] = run
            res["runs"][f"{period} | {vkey(s, system, lev)}"] = rt.summarize(run, data, full)
            if s != "orig":
                rt.write_trades(OUT / "trades" / f"{period}_{s}_{system.replace('+', '_')}_{lev.replace(' ', '_')}.csv",
                                run["trades"])
    bench = {}
    for period in rt.PERIODS:
        t0, t1 = rt.window(full, period)
        dates, rate = full.days[t0:t1 + 1], full.rate[t0:t1 + 1]
        spy, qqq = rt.bench_curve(full, "SPY", t0, t1), rt.bench_curve(full, "QQQ", t0, t1)
        bench[period] = {"SPY": rt.curve_stats(dates, spy, rate, spy, qqq), "QQQ": rt.curve_stats(dates, qqq, rate, spy, qqq),
                         "dates": dates, "spy": spy, "qqq": qqq}
        res["benchmarks_" + period] = {k: bench[period][k] for k in ("SPY", "QQQ")}

    b = bench["full"]
    res["vol_matched"] = {}
    for s, system, lev in variants():
        if lev == "as written":
            dates, eq, rate = rt.curve(runs[("full", s, system, lev)], full)
            res["vol_matched"][vkey(s, system, lev)] = {n: rt.scaled(dates, eq, rate, b[n.upper()]["vol"]) for n in ("spy", "qqq")}
    res["spy_declines"] = []
    for p, q in rt.spy_declines(b["spy"], b["dates"]):
        row = {"peak": str(b["dates"][p]), "trough": str(b["dates"][q]), "SPY": 100 * (b["spy"][q] / b["spy"][p] - 1),
               "QQQ": 100 * (b["qqq"][q] / b["qqq"][p] - 1)}
        for s, system, lev in variants():
            _, eq, _ = rt.curve(runs[("full", s, system, lev)], full)
            row[vkey(s, system, lev)] = 100 * (eq[q] / eq[p] - 1)
        res["spy_declines"].append(row)
    res["cagr_through"] = {}
    for end in ("2019-12-31", "2024-12-31", "2025-12-31"):
        i = int(np.searchsorted(b["dates"], end, side="right")) - 1
        yrs = rt._years(b["dates"][0], b["dates"][i])
        row = {n: 100 * ((b[n.lower()][i] / rt.CAPITAL) ** (1 / yrs) - 1) for n in ("SPY", "QQQ")}
        for s, system, lev in variants():
            _, eq, _ = rt.curve(runs[("full", s, system, lev)], full)
            row[vkey(s, system, lev)] = 100 * ((eq[i] / rt.CAPITAL) ** (1 / yrs) - 1)
        res["cagr_through"][end] = row

    with (OUT / "curves_full.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        vs = variants()
        w.writerow(["date", "SPY", "QQQ"] + [vkey(*v) for v in vs])
        cols = [rt.curve(runs[("full",) + v], full)[1] for v in vs]
        for i, d in enumerate(b["dates"]):
            w.writerow([d, round(b["spy"][i], 2), round(b["qqq"][i], 2)] + [round(c[i], 2) for c in cols])

    # smaller units instead of a cap: the same rules at a fraction of the 1% risk per N, no exposure cap
    res["smaller_units"] = {}
    for s, system in SMALLER:
        data, ind = datas[s]
        for risk in RISKS:
            for period in ("full", "combined"):
                run = rt.run_variant(data, ind, system, "as written", period, risk_per_n=risk, **SETS[s][2])
                res["smaller_units"][f"{period} | {s} {system} {risk}"] = rt.summarize(run, data, full)

    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    (OUT / "report.md").write_text(report(res), encoding="utf-8")
    print("wrote", OUT / "report.md")


# ----- report -----

def cell(c: dict) -> str:
    return f"{rt.f(c['cagr'], 1, '%')} / {rt.f(c['max_dd'], 0, '%')} / {rt.f(c['sharpe'], 2)}"


def detail(res: dict, cols: list[tuple[str, str]], period: str) -> str:
    R = res["runs"]
    hdr = [""] + [h for _, h in cols] + ["SPY", "QQQ"]
    rows = []
    for label, fn in rt.CURVE_ROWS:
        rows.append([label] + [fn(R[f"{period} | {k}"]["curve"]) for k, _ in cols] +
                    [fn(res["benchmarks_" + period][n]) for n in ("SPY", "QQQ")])
    for label, fn in rt.EXPOSURE_ROWS:
        rows.append([label] + [fn(R[f"{period} | {k}"]["exposure"]) for k, _ in cols] + ["", ""])
    for label, fn in rt.TRADE_ROWS:
        rows.append([label] + [fn(R[f"{period} | {k}"]["trades"]) for k, _ in cols] + ["", ""])
    for label, k2 in rt.FLOW_ROWS:
        rows.append([label + " (% of average equity a year)"] +
                    [rt.f(R[f"{period} | {k}"]["flows_pct_per_year"][k2], 2, "%", True) for k, _ in cols] + ["", ""])
    return rt.table(hdr, rows)


def rules_section() -> list[str]:
    return rr.section(
        ["The Original Turtle Trading Rules (the 2003 PDF), as in the first report, traded on ETFs instead of "
         "futures:", "",
         "- N is the 20-day average true range, smoothed Wilder-style. A unit is 1% of the notional account divided "
         "by N, so a move of 1N in one unit is 1% of the account.",
         "- System 1 buys one tick above the 20-day high, or sells short one tick below the 20-day low, but skips the "
         "signal when the previous 20-day breakout would have won; a skipped signal leaves the 55-day breakout as a "
         "failsafe entry. System 2 takes every 55-day breakout. *S1+S2* runs the two systems in separate accounts "
         "with half the money each.",
         "- One unit is added every ½N beyond the previous fill, up to 4 units per market. GLD and SLV, closely "
         "correlated, share a limit of 6 units in one direction (SPY and QQQ did too in the six-ETF version), and "
         "no more than 12 units point one way overall.",
         "- Each unit's stop is 2N from its fill, and earlier stops move up ½N with each add. Positions exit at the "
         "opposite 10-day (System 1) or 20-day (System 2) breakout.",
         "- The notional account resets to equity each January and is cut 20% for each 10% lost, restored when "
         "equity regains the year's start."],
        ["The variations, with the first report's six-ETF long and short runs alongside for reference:", "",
         "- Variation 1: QQQ, GLD, SLV and USO, long and short, all three systems. SPY and TLT are dropped.",
         "- Variation 2: all six ETFs (SPY, QQQ, GLD, SLV, USO, TLT), System 2, long only. Short breakouts are "
         "ignored.",
         "", "Each runs at three leverage settings:", "",
         "- As written: units as the rules size them, borrowing whatever that takes.",
         "- Capped at 2x and at 1x: long plus short value at most 2 or 1 times equity at the previous close. A unit "
         "that does not fit shrinks to the room left and is skipped below a tenth of a unit.",
         "- Smaller units, for comparison: 0.5% or 0.25% of equity per N instead of 1%, with no cap."],
        ["Fills, money and periods:", "",
         "- Orders fill at their trigger, or at the open when the price gaps beyond it. Within a day the price is "
         "assumed to go open, low, high, close on an up day and open, high, low, close on a down day.",
         "- Slippage is 0.10% a side, 0.25% when the as-traded price is under $20. Prices are dividend-adjusted, so "
         "longs earn the distributions and shorts pay them.",
         "- Idle cash and short proceeds earn the 3-month T-bill yield; borrowed cash pays the yield plus 0.5%; shorts "
         "pay 0.5% a year to borrow.",
         "- Each period is a separate run from $100,000 that closes its positions at the end: the full period from "
         "August 2006, 2006–2015, in-sample 2016–2021, out-of-sample 2022 on, and combined 2016 on.",
         "- Benchmarks: SPY and QQQ bought at the first close and held, with dividends."])


def report(res: dict) -> str:
    R = res["runs"]
    L = ["# Turtle variations: without SPY and TLT, and System 2 long only", ""]
    L.append(f"Data through {res['data_through']}. Same rules, costs, money and periods as the first report, "
             "[*The Turtle Trading rules on six ETFs*](/reports?report=turtle-trading-etfs) (its Method section). Variation 1 trades QQQ, GLD, SLV and USO, long and short; "
             "SPY and QQQ were a closely correlated pair, so QQQ now stands alone, and GLD and SLV keep their "
             "6-unit pair limit. Variation 2 trades all six ETFs, System 2, long only: short breakouts are ignored. "
             "Benchmarks: SPY and QQQ with dividends.")
    L.append("")
    L.append("**Both variations remove what lost money in the first report's full-period results, so they are "
             "chosen with hindsight and their full-period numbers are flattered.** The section *Would 2006–2015 have "
             "suggested these changes?* checks the choice against the first half alone.")
    L.append("")
    L += rules_section()
    L.append(findings(res))
    L.append("")
    L.append("![Growth of $100,000 by leverage setting](growth_by_leverage.png)")
    L.append("")
    L.append("## Summary: CAGR / max drawdown / Sharpe")
    L.append("")
    hdr = ["Variant"] + [rt.PERIODS[p][0] for p in rt.PERIODS]
    rows = [[vname(*v)] + [cell(R[f"{p} | {vkey(*v)}"]["curve"]) for p in rt.PERIODS] for v in variants(("v1", "v2"))]
    rows += [[vname(*v) + " (reference)"] + [cell(R[f"{p} | {vkey(*v)}"]["curve"]) for p in rt.PERIODS]
             for v in variants(("orig",))]
    rows += [[f"{n} buy-and-hold"] + [cell(res["benchmarks_" + p][n]) for p in rt.PERIODS] for n in ("SPY", "QQQ")]
    L.append(rt.table(hdr, rows))
    L.append("")
    L += hindsight(res)

    for title, cols in (
            ("Variation 1, without SPY and TLT: rules as written",
             [(vkey("v1", s, "as written"), rt.SYSTEM_NAMES[s]) for s in rt.SYSTEMS]),
            ("Variation 1, without SPY and TLT: gross exposure capped at 2x and 1x",
             [(vkey("v1", s, lev), f"{rt.SYSTEM_NAMES[s]}, {lev}") for lev in ("2x cap", "1x cap") for s in rt.SYSTEMS]),
            ("Variation 2, System 2 long only, by leverage setting",
             [(vkey("v2", "S2", lev), f"Long-only System 2, {lev}") for lev in rt.LEVERAGE] +
             [(vkey("orig", "S2", "as written"), "Long/short System 2, as written (reference)")]),
    ):
        for p in ("full", "combined"):
            L.append(f"## {title}: {rt.PERIODS[p][0].lower()}")
            L.append("")
            L.append(detail(res, cols, p))
            L.append("")

    L += smaller_units(res)

    # yearly
    L.append("## Calendar years (full-period runs)")
    L.append("")
    cols = [vkey("v1", "S2", lev) for lev in rt.LEVERAGE] + [vkey("v1", "S1+S2", "as written")] + \
        [vkey("v2", "S2", lev) for lev in rt.LEVERAGE] + [vkey("orig", "S2", "as written")]
    heads = [f"No SPY/TLT S2, {lev}" for lev in rt.LEVERAGE] + ["No SPY/TLT half each, as written"] + \
        [f"Long-only S2, {lev}" for lev in rt.LEVERAGE] + ["All six L/S S2, as written"]
    years = list(R[f"full | {cols[0]}"]["curve"]["yearly"])
    rows = [[y] + [rt.f(R[f"full | {k}"]["curve"]["yearly"][y], 1, "%") for k in cols] +
            [rt.f(res["benchmarks_full"][n]["yearly"][y], 1, "%") for n in ("SPY", "QQQ")] for y in years]
    L.append(rt.table(["Year"] + heads + ["SPY", "QQQ"], rows))
    L.append("")

    # CAGR through earlier year-ends
    L.append("## CAGR from August 2006 through earlier year-ends")
    L.append("")
    sel = [vkey("v1", s, lev) for s in ("S2", "S1+S2") for lev in rt.LEVERAGE] + \
        [vkey("v2", "S2", lev) for lev in rt.LEVERAGE] + [vkey("orig", "S2", lev) for lev in rt.LEVERAGE]
    hdr = ["Through"] + [SHORT[k.split()[0]] + " " + k.split(" ", 1)[1] for k in sel] + ["SPY", "QQQ"]
    rows = [[end] + [rt.f(row[k], 1, "%") for k in sel] + [rt.f(row[n], 1, "%") for n in ("SPY", "QQQ")]
            for end, row in res["cagr_through"].items()]
    rows.append(["2026-09-30"] + [rt.f(R[f"full | {k}"]["curve"]["cagr"], 1, "%") for k in sel] +
                [rt.f(res["benchmarks_full"][n]["cagr"], 1, "%") for n in ("SPY", "QQQ")])
    L.append(rt.table(hdr, rows))
    L.append("")

    # declines
    L.append("## During SPY's declines of 15% or more (full-period runs, peak close to trough close)")
    L.append("")
    sel = [vkey("v1", s, lev) for s in ("S2", "S1+S2") for lev in rt.LEVERAGE] + [vkey("v2", "S2", lev) for lev in rt.LEVERAGE]
    hdr = ["SPY peak", "SPY trough", "SPY", "QQQ"] + [SHORT[k.split()[0]] + " " + k.split(" ", 1)[1] for k in sel]
    rows = [[d["peak"], d["trough"], rt.f(d["SPY"], 1, "%"), rt.f(d["QQQ"], 1, "%")] +
            [rt.f(d[k], 1, "%", True) for k in sel] for d in res["spy_declines"]]
    L.append(rt.table(hdr, rows))
    L.append("")

    # vol matched
    L.append("## At the same volatility as SPY or QQQ (full period, rules as written)")
    L.append("")
    L.append("Daily excess returns over T-bills scaled to the benchmark's full-period volatility; a risk-adjusted "
             "comparison, not a tradable setting.")
    L.append("")
    hdr = ["", "Scale (x the 1% per N)", "CAGR", "Max drawdown", "Sharpe", "Benchmark CAGR / max drawdown"]
    rows = []
    for k, m in res["vol_matched"].items():
        s, system, lev = k.split(" ")[0], k.split(" ")[1], "as written"
        for n in ("spy", "qqq"):
            x, bc = m[n], res["benchmarks_full"][n.upper()]
            rows.append([f"{vname(s, system, lev)}, at {n.upper()}'s volatility", f"{x['k']:.2f}", rt.f(x["cagr"], 1, "%"),
                         rt.f(x["max_dd"], 1, "%"), rt.f(x["sharpe"], 2), f"{rt.f(bc['cagr'], 1, '%')} / {rt.f(bc['max_dd'], 1, '%')}"])
    L.append(rt.table(hdr, rows))
    L.append("")

    # where the profit came from
    L.append("## Trade profit by market and side (full period, rules as written)")
    L.append("")
    sel = [vkey("v1", s, "as written") for s in rt.SYSTEMS] + [vkey("v2", "S2", "as written"), vkey("orig", "S2", "as written")]
    hdr = ["Market"] + [SHORT[k.split()[0]] + " " + k.split(" ", 1)[1] for k in sel]
    rows = [[sym] + [f"${R[f'full | {k}']['trades']['by_market'].get(sym, 0.0):,.0f}"
                     if sym in SETS[k.split()[0]][1] else "not traded" for k in sel] for sym in td.SYMBOLS]
    rows.append(["Long trades"] + [f"${R[f'full | {k}']['trades']['long_pnl']:,.0f}" for k in sel])
    rows.append(["Short trades"] + [f"${R[f'full | {k}']['trades']['short_pnl']:,.0f}" for k in sel])
    L.append(rt.table(hdr, rows))
    L.append("")
    L.append("## Files")
    L.append("")
    L.append("Trade lists: `trades/<period>_<variation>_<system>_<leverage>.csv` (v1 = without SPY and TLT, v2 = long "
             "only). Daily values for every full-period run: `curves_full.csv`.")
    return "\n".join(L)


def hindsight(res: dict) -> list[str]:
    R = res["runs"]
    L = ["## Would 2006–2015 have suggested these changes?", ""]
    L.append("The first report's 2006–2015 run, on its own: trade profit by market and by side. If the first half "
             "already pointed to dropping SPY and TLT, or the short side, the 2016–present results are a fairer test "
             "of the change.")
    L.append("")
    sel = [(vkey("orig", s, lev), f"{rt.SYSTEM_NAMES[s]}, {lev}") for lev in rt.LEVERAGE for s in rt.SYSTEMS]
    hdr = ["2006–2015 trade profit"] + [h for _, h in sel]
    rows = [[sym] + [f"${R[f'early | {k}']['trades']['by_market'].get(sym, 0.0):,.0f}" for k, _ in sel]
            for sym in td.SYMBOLS]
    rows.append(["Long trades"] + [f"${R[f'early | {k}']['trades']['long_pnl']:,.0f}" for k, _ in sel])
    rows.append(["Short trades"] + [f"${R[f'early | {k}']['trades']['short_pnl']:,.0f}" for k, _ in sel])
    L.append(rt.table(hdr, rows))
    L.append("")
    L.append("From 2016 (each a separate run from $100,000, CAGR / max drawdown / Sharpe):")
    L.append("")
    hdr = ["System and leverage", "All six, long and short", "Without SPY and TLT", "Long only", "SPY", "QQQ"]
    rows = []
    for s in rt.SYSTEMS:
        for lev in rt.LEVERAGE:
            c = lambda st: cell(R[f"combined | {vkey(st, s, lev)}"]["curve"]) if s in SETS[st][3] else "–"
            rows.append([f"{rt.SYSTEM_NAMES[s]}, {lev}", c("orig"), c("v1"), c("v2"),
                         cell(res["benchmarks_combined"]["SPY"]), cell(res["benchmarks_combined"]["QQQ"])])
    L.append(rt.table(hdr, rows))
    L.append("")
    return L


def smaller_units(res: dict) -> list[str]:
    S = res["smaller_units"]
    L = ["## Lower leverage by smaller units instead of a cap", ""]
    L.append("A gross-exposure cap shrinks or skips the units that do not fit, which mostly cuts the add-on units of "
             "positions already running, where the profit is. The alternative is a smaller unit: the same rules with "
             "less than 1% of equity per N and no cap. 0.5% and 0.25% are round numbers, not fitted values. Gross "
             "exposure still varies with volatility, so the peaks exceed what a margin account allows.")
    L.append("")
    hdr = ["Variation", "Risk per N", "Full: CAGR / max DD / Sharpe", "Full: gross avg / most",
           "2016–present: CAGR / max DD / Sharpe", "2016–present: gross avg / most"]
    rows = []
    for s, system in SMALLER:
        for risk in RISKS:
            a, b = S[f"full | {s} {system} {risk}"], S[f"combined | {s} {system} {risk}"]
            name = f"{SETS[s][0]}: {'System 2' if s == 'v2' else rt.SYSTEM_NAMES[system]}"
            rows.append([name, f"{100 * risk:g}%", cell(a["curve"]),
                         f"{a['exposure']['avg_gross']:.2f}x / {a['exposure']['max_gross']:.2f}x",
                         cell(b["curve"]), f"{b['exposure']['avg_gross']:.2f}x / {b['exposure']['max_gross']:.2f}x"])
    for n in ("SPY", "QQQ"):
        rows.append([f"{n} buy-and-hold", "", cell(res["benchmarks_full"][n]), "1.00x", cell(res["benchmarks_combined"][n]), "1.00x"])
    L.append(rt.table(hdr, rows))
    L.append("")
    return L


def money(x: float) -> str:
    return f"made ${x:,.0f}" if x >= 0 else f"lost ${-x:,.0f}"


def pct_move(x: float) -> str:
    return f"gained {x:.0f}%" if x >= 0 else f"lost {-x:.0f}%"


def findings(res: dict) -> str:
    R, S = res["runs"], res["smaller_units"]

    def c(k, p="full"):
        return R[f"{p} | {k}"]["curve"]

    def su(k, risk, p="full"):
        return S[f"{p} | {k} {risk}"]

    spy, qqq = res["benchmarks_full"]["SPY"], res["benchmarks_full"]["QQQ"]
    spy16, qqq16 = res["benchmarks_combined"]["SPY"], res["benchmarks_combined"]["QQQ"]
    thr = res["cagr_through"]["2024-12-31"]
    levs = list(rt.LEVERAGE)
    v1 = {lev: c(f"v1 S2 {lev}") for lev in levs}
    v1c = {lev: c(f"v1 S2 {lev}", "combined") for lev in levs}
    v2 = {lev: c(f"v2 S2 {lev}") for lev in levs}
    v2c = {lev: c(f"v2 S2 {lev}", "combined") for lev in levs}
    o2c = {lev: c(f"orig S2 {lev}", "combined") for lev in levs}
    early = {(s, lev): R[f"early | orig {s} {lev}"]["trades"] for s in rt.SYSTEMS for lev in levs}
    dropped_lost = sum(early[k]["by_market"].get("SPY", 0) < 0 and early[k]["by_market"].get("TLT", 0) < 0 for k in early)
    v1_wins16 = sum(c(f"v1 {s} {lev}", "combined")["cagr"] > c(f"orig {s} {lev}", "combined")["cagr"]
                    for s in rt.SYSTEMS for lev in levs)
    t1 = R["full | v1 S2 as written"]["trades"]
    oil_share = 100 * t1["by_market"]["USO"] / t1["total_pnl"]
    corr_v1 = [v1[lev]["corr_spy_monthly"] for lev in levs]
    declines = res["spy_declines"]
    v1_all_up = all(d[f"v1 S2 {lev}"] > 0 for d in declines for lev in levs)
    covid = next(d for d in declines if d["peak"].startswith("2020"))
    shorts_early = {lev: early[("S2", lev)]["short_pnl"] for lev in levs}

    def trio(d, digits=1):
        return " / ".join(f"{d[lev]['cagr']:.{digits}f}%" for lev in levs)

    lines = [
        "## Findings",
        "",
        "CAGR triples below are rules as written / capped at 2x / capped at 1x.",
        "",
        f"1. **Without SPY and TLT, System 2 did well at every leverage setting.** Full period: {trio(v1)} a year, "
        f"with max drawdowns of {v1['as written']['max_dd']:.0f}% / {v1['2x cap']['max_dd']:.0f}% / "
        f"{v1['1x cap']['max_dd']:.0f}%, against SPY's {spy['cagr']:.1f}% ({spy['max_dd']:.0f}%) and QQQ's "
        f"{qqq['cagr']:.1f}% ({qqq['max_dd']:.0f}%). Sharpe {v1['as written']['sharpe']:.2f} at each setting (SPY "
        f"{spy['sharpe']:.2f}, QQQ {qqq['sharpe']:.2f}). From 2016: {trio(v1c)}, against SPY {spy16['cagr']:.1f}% and "
        f"QQQ {qqq16['cagr']:.1f}%. Unlike the six-ETF version, it was ahead of SPY before 2025 as well (August 2006 "
        f"through 2024: {thr['v1 S2 as written']:.1f}% / {thr['v1 S2 2x cap']:.1f}% / {thr['v1 S2 1x cap']:.1f}%, "
        f"SPY {thr['SPY']:.1f}%, QQQ {thr['QQQ']:.1f}%).",
        f"2. **The first half supports dropping SPY and TLT, but the result leans on oil.** In 2006–2015 SPY and TLT "
        f"both lost money in {dropped_lost} of the 9 system and leverage combinations, and from 2016 the four-market "
        f"version beat the six-market one in {v1_wins16} of 9. USO made {oil_share:.0f}% of System 2's trade profit "
        f"as written, and the best single trade was {t1['best_share']:.0f}% of it. Four markets, one of them equities, "
        f"is thin diversification, and the next decade's trends may come from the markets dropped.",
        f"3. **It kept the crash protection.** Monthly correlation to SPY was {min(corr_v1):.2f} to {max(corr_v1):.2f}, "
        + ("and it made money in all five SPY declines of 15% or more at every leverage setting."
           if v1_all_up else "and it made money in most SPY declines of 15% or more."),
        f"4. **Long-only System 2 needs the leverage.** Full period: {trio(v2)} a year (drawdowns "
        f"{v2['as written']['max_dd']:.0f}% / {v2['2x cap']['max_dd']:.0f}% / {v2['1x cap']['max_dd']:.0f}%). As "
        f"written it turned $100,000 into ${v2['as written']['final'] / 1e6:.1f}M, but both capped versions trail SPY. "
        f"From 2016: {trio(v2c)}, against long/short's {trio(o2c)} and SPY's {spy16['cagr']:.1f}%.",
        f"5. **The first half did not clearly point to long only, and it gives up the crash protection.** In 2006–2015 "
        f"System 2's shorts {money(shorts_early['as written'])} as written, {money(shorts_early['2x cap'])} capped at "
        f"2x and {money(shorts_early['1x cap'])} at 1x. From February to March 2020, as written, long-only "
        f"{pct_move(covid['v2 S2 as written'])} while the long/short version {pct_move(covid['orig S2 as written'])} and "
        f"SPY fell {-covid['SPY']:.0f}%. Monthly correlation to SPY turned positive "
        f"({v2['as written']['corr_spy_monthly']:+.2f}).",
        f"6. **For long only, smaller units beat a cap; for the four-market version, the cap did better.** At 0.25% of "
        f"equity per N (about {su('v2 S2', 0.0025)['exposure']['avg_gross']:.1f}x average gross, peaks "
        f"{su('v2 S2', 0.0025)['exposure']['max_gross']:.1f}x) long-only made "
        f"{su('v2 S2', 0.0025)['curve']['cagr']:.1f}%/yr with a {su('v2 S2', 0.0025)['curve']['max_dd']:.0f}% drawdown, "
        f"against {v2['1x cap']['cagr']:.1f}% and {v2['1x cap']['max_dd']:.0f}% with full units capped at 1x. A full "
        f"account refuses new units, most of them add-ons to positions already moving the right way (the first "
        f"report's *Orders not placed* table), while smaller units keep every add. Without SPY and TLT, 0.25% per N made "
        f"{su('v1 S2', 0.0025)['curve']['cagr']:.1f}%/yr against {v1['1x cap']['cagr']:.1f}% with the 1x cap. At 0.5% "
        f"per N (about 1.7–2x average gross) the two variations made {su('v1 S2', 0.005)['curve']['cagr']:.1f}% and "
        f"{su('v2 S2', 0.005)['curve']['cagr']:.1f}%.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
