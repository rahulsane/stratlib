"""The report for run_ma_pullback.py (output/ma_pullback/report.md), written from its results.json."""

from __future__ import annotations

from engine import PERIOD_TITLES
from run_ma_pullback import EXITS, STOPS, TRENDS, UNIVERSES, VARIANTS, neighbours
from run_wtt import f, table

SHORT_EXIT = {"target": "Target", "trail": "EMA exit"}
SHORT_STOP = {"swing": "Swing-low stop", "entry": "Entry stop"}
SHORT_TREND = {"above": "above 200", "rising": "rising 200"}
GENERIC = [   # robustness rows: label -> how to find it in each variant's checks
    ("Double costs", lambda v: "double costs"),
    ("Entries a session late", lambda v: "entries a session late"),
    ("Area 0.25 ATR either side", lambda v: "area 0.25 ATR either side"),
    ("Area 0.75 ATR either side", lambda v: "area 0.75 ATR either side"),
    ("40-day EMA (area and exit)", lambda v: "40-day EMA (area and exit)"),
    ("60-day EMA (area and exit)", lambda v: "60-day EMA (area and exit)"),
    ("Higher-close trigger", lambda v: "higher-close trigger"),
    ("Stop one notch tighter (0.5 ATR below the low / 1.5 ATR below the entry)",
     lambda v: "stop 0.5 ATR below the low" if VARIANTS[v]["stop"] == "swing" else "stop 1.5 ATR below the entry"),
    ("Stop one notch looser (1.5 / 2.5 ATR)",
     lambda v: "stop 1.5 ATR below the low" if VARIANTS[v]["stop"] == "swing" else "stop 2.5 ATR below the entry"),
    ("Target at the swing high", lambda v: "target at the swing high"),
    ("Target 0.5 ATR below it", lambda v: "target 0.5 ATR below it"),
    ("200-day EMA rising over 10 sessions", lambda v: "200-day EMA rising over 10 sessions"),
    ("200-day EMA rising over 40 sessions", lambda v: "200-day EMA rising over 40 sessions"),
]


def label(v: str) -> str:
    x = VARIANTS[v]
    return f"{SHORT_EXIT[x['exit']]}, {SHORT_STOP[x['stop']].lower()}, {SHORT_TREND[x['trend']]}"


def head(v: str) -> str:
    x = VARIANTS[v]
    return f"{SHORT_EXIT[x['exit']]} / {x['stop']} / {x['trend']}"


def rule_cells(v: str) -> list[str]:
    x = VARIANTS[v]
    return [SHORT_EXIT[x["exit"]], SHORT_STOP[x["stop"]], SHORT_TREND[x["trend"]].capitalize()]


RULE_HEAD = ["Exit", "Stop", "Trend filter"]


def pm(x: float, se: float, digits: int = 2) -> str:
    return f"{f(x, digits, sign=True)} ± {f(se, digits)}"


def rng(values, digits: int = 1, suffix: str = "%", sign: bool = False) -> str:
    lo, hi = min(values), max(values)
    if round(lo, digits) == round(hi, digits):
        return f"{f(lo, digits, sign=sign)}{suffix}"
    return f"{f(lo, digits, sign=sign)} to {f(hi, digits, sign=sign)}{suffix}"


def findings(res: dict) -> str:
    cf, spy, mc, rb, ev = res["configs"], res["spy"], res["mc"], res["robust"], res["every"]
    sp, sa = spy["combined/pre"], spy["combined/after"]
    keys = {u: [f"{v}/{u}" for v in VARIANTS] for u in UNIVERSES}
    allk = keys["sp900"] + keys["r3000"]
    by_exit = {x: [k for k in allk if VARIANTS[k.split("/")[0]]["exit"] == x] for x in EXITS}

    def cg(ks, key):
        return [cf[k]["cagr"][key] for k in ks]

    def tr(ks, key):
        return [cf[k]["trades"][key] for k in ks]

    def period(ks, name):
        return [cf[k]["periods"][name]["pre"]["cagr"] for k in ks]

    years = (res["data_through"], "2016-01-04")
    span = (int(years[0][:4]) - 2016) + (int(years[0][5:7]) - 1) / 12 + 1 / 12
    per_year = {u: [ev[k]["per_year"] for k in keys[u]] for u in UNIVERSES}
    rising_cut = [100 * (1 - ev[f"{x}-{s}-rising/{u}"]["trades"] / ev[f"{x}-{s}-above/{u}"]["trades"])
                  for x in EXITS for s in STOPS for u in UNIVERSES]
    pair_gap = [abs(cf[f"{x}-swing-{t}/{u}"]["cagr"]["pre"] - cf[f"{x}-entry-{t}/{u}"]["cagr"]["pre"])
                for x in EXITS for t in TRENDS for u in UNIVERSES]
    best_ev = max(allk, key=lambda k: ev[k]["expectancy_r"])
    pi, po = cf[allk[0]]["periods"]["in_sample"], cf[allk[0]]["periods"]["out_of_sample"]
    robust_cells = [(k, lab, x) for k in allk for lab, x in rb[k].items()]
    best = max(robust_cells, key=lambda z: z[2]["pre"])
    worse_hc = sum(rb[k]["higher-close trigger"]["pre"] < cf[k]["cagr"]["pre"] for k in allk)
    r3_trail = [k for k in keys["r3000"] if VARIANTS[k.split("/")[0]]["exit"] == "trail"]
    drag = [cf[k]["cagr"]["pre"] - cf[k]["cagr"]["after_held"] for k in allk]
    trail_r3_drag = [cf[k]["cagr"]["pre"] - cf[k]["cagr"]["after_held"] for k in r3_trail]
    carry = [cf[k]["tax"]["carry_st"] for k in allk]
    lev = [cf[k]["leverage_check"] for k in allk]
    tgt_sp = [k for k in keys["sp900"] if VARIANTS[k.split("/")[0]]["exit"] == "target"]
    tgt_r3 = [k for k in keys["r3000"] if VARIANTS[k.split("/")[0]]["exit"] == "target"]
    yr23 = [cf[k]["pre"]["yearly"]["2023"] for k in keys["sp900"]]
    sb = res["spy_block"]["pre"]
    del span
    cost_hit = [cf[k]["cagr"]["pre"] - rb[k]["double costs"]["pre"] for k in allk]
    trend_gap = [abs(cf[f"{x}-{s}-above/{u}"]["cagr"]["pre"] - cf[f"{x}-{s}-rising/{u}"]["cagr"]["pre"])
                 for x in EXITS for s in STOPS for u in UNIVERSES]
    break_even = [100 * -c["trades"]["avg_loss_r"] / (c["trades"]["avg_win_r"] - c["trades"]["avg_loss_r"])
                  for c in (cf[k] for k in by_exit["target"])]
    sold_same = all(abs(cf[k]["cagr"]["after_held"] - cf[k]["cagr"]["after_sold"]) < 0.05 for k in allk)
    items = [
        f"**None of the 16 versions beat SPY, before or after tax.** Before tax the eight versions made "
        f"{rng(cg(keys['sp900'], 'pre'))} a year on the S&P 500 + MidCap 400 and {rng(cg(keys['r3000'], 'pre'))} on "
        f"the Russell 3000, against SPY's {f(sp['cagr_held'])}% with dividends. After tax with positions held they "
        f"made {rng(cg(allk, 'after_held'))} against SPY's {f(sa['cagr_held'])}%"
        + (f"; selling everything at the end changes nothing for them (their loss carryforwards absorb the gains), "
           f"while SPY sold at the end makes {f(sa['cagr_sold'])}%. " if sold_same else
           f" (sold at the end: {rng(cg(allk, 'after_sold'))} against {f(sa['cagr_sold'])}%). ")
        + f"None of the {res['seeds']} random selections "
        f"behind each version beat SPY either; the best 95th percentile was "
        f"{f(max(mc[k + '/pre']['p95'] for k in allk))}%.",
        f"**Per trade, the edge is about the size of the trading costs.** Taking every signal with no portfolio "
        f"limits (about {f(min(per_year['sp900']), 0)} a year on the S&P 900 and {f(min(per_year['r3000']), 0)} on the "
        f"Russell 3000), the average trade made {rng([ev[k]['expectancy_r'] for k in allk], 3, 'R', True)} after "
        "slippage. "
        f"The best was {f(ev[best_ev]['expectancy_r'], 3, sign=True)} ± {f(ev[best_ev]['expectancy_se'], 3)}R "
        f"(t = {f(ev[best_ev]['t_stat'], 1)}), and every Russell 3000 version was negative. Before costs the range "
        f"was {rng([ev[k]['expectancy_before_costs_r'] for k in allk], 3, 'R', True)}: the stops sit a few percent below "
        f"the entry, so 0.2% of slippage a round trip costs about {f(min(tr(allk, 'cost_r')), 3)}–"
        f"{f(max(tr(allk, 'cost_r')), 3)}R a trade. Every signal trailed SPY over the same sessions, by "
        f"{f(-max(ev[k]['spy_excess_pct'] for k in allk), 2)}% to {f(-min(ev[k]['spy_excess_pct'] for k in allk), 2)}% "
        f"a trade on average. Doubling the costs took {rng(cost_hit, 1, ' points')} a year off the versions' CAGR.",
        f"**Of the three disagreements, only the exit matters.** The target exit wins often but small: "
        f"{rng(tr(by_exit['target'], 'win_rate'), 0)} of trades win, the average win is "
        f"{rng(tr(by_exit['target'], 'avg_win_r'), 2, 'R')} against an average loss of "
        f"{rng([-x for x in tr(by_exit['target'], 'avg_loss_r')], 2, 'R')} (a payoff of "
        f"{rng(tr(by_exit['target'], 'payoff'), 2, '')}, which needs {rng(break_even, 0)} winners to break even), "
        f"trades last {rng(tr(by_exit['target'], 'hold'), 0, ' sessions')} on average, and the portfolio's expectancy "
        f"was {rng(tr(by_exit['target'], 'expectancy_r'), 2, 'R', True)}. Only "
        f"{rng(tr(by_exit['target'], 'losers_up_1r'), 0)} "
        f"of its losers were ever 1R ahead. The 50-day EMA exit is the opposite: "
        f"{rng(tr(by_exit['trail'], 'win_rate'), 0)} winners, a payoff of {rng(tr(by_exit['trail'], 'payoff'), 2, '')}, "
        f"winners held {f(min(tr(by_exit['trail'], 'hold_win')), 0)}–{f(max(tr(by_exit['trail'], 'hold_win')), 0)} "
        f"sessions and losers {f(min(tr(by_exit['trail'], 'hold_loss')), 0)}, expectancy "
        f"{rng(tr(by_exit['trail'], 'expectancy_r'), 2, 'R', True)}, a profit factor of "
        f"{rng(tr(by_exit['trail'], 'profit_factor'), 2, '')}, and losing streaks of up to "
        f"{max(tr(by_exit['trail'], 'max_losing_streak'))} trades in a row. Swapping the stop changed a version's CAGR "
        f"by at most {f(max(pair_gap), 1)} points, and swapping the trend filter by at most {f(max(trend_gap), 1)}. "
        f"Requiring a rising 200-day EMA removed only {f(min(rising_cut), 1)}–"
        f"{f(max(rising_cut), 1)}% of the signals: a stock on its third bounce above the 200-day EMA nearly always "
        "has that average rising.",
        f"**It made money before 2022 and has lost money since.** Run on 2016–2021 alone, the EMA-exit versions made "
        f"{rng(period(by_exit['trail'], 'in_sample'))} a year against SPY's {f(pi['spy_pre'])}%, and the target "
        f"versions {rng(period(by_exit['target'], 'in_sample'))} (survivorship flatters these years, most for the "
        f"Russell 3000). From 2022 every one of the 16 lost money: {rng(period(allk, 'out_of_sample'))} a year, "
        f"while SPY made {f(po['spy_pre'])}%. In 2023 the S&P 900 versions lost {f(-max(yr23), 0)}–"
        f"{f(-min(yr23), 0)}% while SPY gained {f(sb['yearly']['2023'], 0)}%.",
        f"**Almost every gain is short-term, so taxes take up to {f(max(drag), 1)} points a year.** "
        f"{rng([cf[k]['tax']['st_share_of_gains'] for k in allk], 0)} of realized gains were short-term (taxed at "
        f"40.8%). The tax drag with positions held was {rng(drag, 1, ' points')}, against SPY's "
        f"{f(sp['cagr_held'] - sa['cagr_held'], 1)}. It is largest for the EMA exit on the Russell 3000 "
        f"({f(min(trail_r3_drag), 1)}–{f(max(trail_r3_drag), 1)} points): tax was paid on the gains of 2016–2021, "
        f"and the losses since then were left as carryforwards (${min(carry):,.0f}–${max(carry):,.0f} unused at the "
        "end across the 16 versions), which only help a reader with other gains to offset.",
        f"**Calmer than SPY, but SPY at the same volatility made more.** The target versions on the S&P 900 had "
        f"{rng([cf[k]['pre']['vol'] for k in tgt_sp], 0)} volatility against SPY's {f(sb['vol'], 0)}%, the rest "
        f"{rng([cf[k]['pre']['vol'] for k in allk if k not in tgt_sp], 0)}. Maximum drawdowns were "
        f"{rng([cf[k]['pre']['max_drawdown'] for k in allk], 0)} (SPY {f(sb['max_drawdown'], 0)}%), and the Russell "
        f"3000 target versions spent {f(min(cf[k]['pre']['longest_underwater_months'] for k in tgt_r3), 0)}–"
        f"{f(max(cf[k]['pre']['longest_underwater_months'] for k in tgt_r3), 0)} months below a previous high, "
        f"almost the whole test. SPY held at each version's volatility ({f(min(x['leverage'] for x in lev), 2)}–"
        f"{f(max(x['leverage'] for x in lev), 2)}x, partly in cash for the calmer versions) made "
        f"{rng([x['pre'] for x in lev])} before tax and {rng([x['after_held'] for x in lev])} after.",
        f"**The judgement calls matter more than the disagreements, and the best result leans on the ranking.** "
        f"Moving each loose rule one notch gave {rng([x['pre'] for _, _, x in robust_cells])} a year before tax. "
        f"The best cell ({label(best[0].split('/')[0])}, {UNIVERSES[best[0].split('/')[1]]}, {best[1]}) made "
        f"{f(best[2]['pre'])}% before tax and {f(best[2]['after_held'])}% after, still below SPY. The Russell 3000 "
        f"EMA-exit versions ranked at the {rng([mc[k + '/pre']['ranked_percentile'] for k in r3_trail], 0, 'th')} "
        f"percentile of the random selections (random medians {rng([mc[k + '/pre']['p50'] for k in r3_trail])}); "
        f"without their five largest winners they made {rng([cf[k]['without_top5'] for k in r3_trail])}. His "
        f"simplest trigger, any higher close, did worse than the hammer and engulfing candles in {worse_hc} of 16 "
        "versions.",
    ]
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))


def report(res: dict) -> str:
    cf, spy, mc, rb, ev, sb = res["configs"], res["spy"], res["mc"], res["robust"], res["every"], res["spy_block"]
    sp, sa = spy["combined/pre"], spy["combined/after"]
    out = ["# Rayner Teo's moving-average pullback, before and after tax", "",
           f"Data through {res['data_through']}. "
           "The rules, the eight variants and the ranking were fixed before any results. Method and assumptions at "
           "the end.", "", "## Findings", "", findings(res).strip(), "",
           "![After-tax growth and drawdowns](equity_drawdown.png)", ""]

    # ------------------------------------------------------------------ rules
    out += ["## The rules tested", "",
            "From his articles (sources at the end). Daily bars, long only:", "",
            "1. **Area of value**: the 50-day EMA, plus or minus half a 20-day ATR (\"an area, not a line\").",
            "2. **Third touch**: count pullbacks into the area. A pullback is a completed test when price then makes "
            "a new high above the swing high before it; a close below the area resets the count. Trade only the "
            "third pullback, after two completed tests.",
            "3. **Trigger**: a hammer or a bullish engulfing candle, as his candlestick guide defines them, on a "
            "session that reaches the area (or the one after). Buy at the next open; one entry per pullback.",
            "4. **Size**: 1% of the account at risk (his rule), at most 20% of the account in one stock, 10 "
            "positions, no margin.", "",
            "His articles disagree on three rules, so all eight combinations ran:", "",
            table(["Rule", "Version 1", "Version 2"], [
                ["Trend filter", TRENDS["above"], TRENDS["rising"] + " (over 20 sessions)"],
                ["Initial stop", STOPS["swing"] + " of the pullback", STOPS["entry"] + " price"],
                ["Exit", "Target 0.25 ATR below the swing high before the pullback; the stop stays put",
                 "Sell at the next open after a close below the 50-day EMA; the stop stays as a hard stop"]]), ""]

    for u, uname in UNIVERSES.items():
        keys = [f"{v}/{u}" for v in VARIANTS]
        # -------------------------------------------------------------- headline
        rows = [rule_cells(k.split("/")[0]) + [
            f"**{f(cf[k]['cagr']['pre'])}%**", f"{f(cf[k]['cagr']['after_held'])}%", f"{f(cf[k]['cagr']['after_sold'])}%",
            f"{f(cf[k]['cagr']['pre'] - cf[k]['cagr']['after_held'], 1)} pts", f"{f(cf[k]['pre']['vol'], 0)}%",
            f"{f(cf[k]['pre']['max_drawdown'], 0)}%", f"{f(cf[k]['invested'], 0)}%"] for k in keys]
        rows.append(["SPY", "", "", f"**{f(sp['cagr_held'])}%**", f"{f(sa['cagr_held'])}%", f"{f(sa['cagr_sold'])}%",
                     f"{f(sp['cagr_held'] - sa['cagr_held'], 1)} pts", f"{f(sb['pre']['vol'], 0)}%",
                     f"{f(sb['pre']['max_drawdown'], 0)}%", "100%"])
        out += [f"## {uname}", "", "### Before and after tax", "",
                "Combined period (2016 to the last close), ranked by 12-month momentum, the rule chosen before the "
                "run. *Held*: taxes paid on everything realized, open positions not sold. *Sold*: everything sold at "
                "the end and taxed. Volatility and drawdown before tax.", "",
                table(RULE_HEAD + ["Before tax", "After tax, held", "After tax, sold", "Tax drag (held)", "Volatility",
                                   "Max drawdown", "Average invested"], rows), ""]

        # -------------------------------------------------------------- trades
        tr = {k: cf[k]["trades"] for k in keys}
        rows = [rule_cells(k.split("/")[0]) + [
            f"{tr[k]['trades']:,} ({f(tr[k]['per_year'], 0)})", f"{f(tr[k]['win_rate'], 1)}%",
            f"{f(tr[k]['avg_win_r'], 2, 'R')} / {f(tr[k]['avg_loss_r'], 2, 'R')}", f(tr[k]["payoff"], 2),
            f"**{pm(tr[k]['expectancy_r'], tr[k]['expectancy_se'])}R**", f"{f(tr[k]['expectancy_before_costs_r'], 2, 'R', sign=True)}",
            f"{f(tr[k]['expectancy_pct'], 2, '%', sign=True)}", f"{f(tr[k]['spy_excess_pct'], 2, '%', sign=True)}",
            f(tr[k]["profit_factor"], 2), f(tr[k]["sqn"], 2), f"{f(tr[k]['kelly'], 1, '%')}"] for k in keys]
        out += ["### Trade statistics", "",
                "The portfolio's own trades, combined period, before tax. *Expectancy*: the average trade in R (R = "
                "the distance from the entry fill to the initial stop), ± one standard error; before costs adds back "
                "the slippage. *vs SPY*: the trade's return minus SPY's over the same sessions. *SQN*: expectancy / "
                "standard deviation of R × √(trades, at most 100). *Kelly*: win rate − loss rate / payoff, the "
                "fraction of capital the edge would justify risking per trade (negative means none).", "",
                table(RULE_HEAD + ["Trades (a year)", "Win rate", "Average win / loss", "Payoff", "Expectancy",
                                   "Before costs", "Average trade", "vs SPY", "Profit factor", "SQN", "Kelly"], rows), ""]
        rows = []
        for k in keys:
            t = tr[k]
            ex = t["exits"]
            other = 100 - ex.get("stop", 0) - ex.get("target", 0) - ex.get("close below the 50-day EMA", 0)
            rows.append(rule_cells(k.split("/")[0]) + [
                f"{f(t['hold'], 0)} ({f(t['hold_median'], 0)})", f"{f(t['hold_win'], 0)} / {f(t['hold_loss'], 0)}",
                f"{f(ex.get('stop', 0), 0)}% / {f(ex.get('target', 0), 0)}% / {f(ex.get('close below the 50-day EMA', 0), 0)}% / {f(other, 0)}%",
                f"{f(t['mae_win'], 2, 'R')}", f"{f(t['mfe_loss'], 2, 'R')}", f"{f(t['losers_up_1r'], 0)}%",
                str(t["max_losing_streak"]), f"{f(t['best_r'], 1, 'R')} / {f(t['worst_r'], 1, 'R')}",
                f"{f(t['cost_r'], 3, 'R')}", f"{f(cf[k]['avg_positions'], 1)}"])
        out += ["Holding and exits:", "",
                table(RULE_HEAD + ["Sessions held: average (median)", "Winners / losers", "Exits: stop / target / EMA / other",
                                   "Winners' worst point", "Losers' best point", "Losers that were 1R ahead",
                                   "Longest losing streak", "Best / worst trade", "Slippage per trade", "Average positions"],
                      rows), "",
                "*Worst point* and *best point*: the lowest low (maximum adverse excursion) and highest high (maximum "
                "favourable excursion) while the trade was open, in R. *Other* exits: delistings, the end of the test "
                "and gaps through the stop are counted with the stop.", ""]

        # -------------------------------------------------------------- every signal
        rows = []
        for v in VARIANTS:
            e = ev[f"{v}/{u}"]
            rows.append(rule_cells(v) + [
                f"{e['trades']:,} ({f(e['per_year'], 0)})", f"{f(e['win_rate'], 1)}%",
                f"**{pm(e['expectancy_r'], e['expectancy_se'], 3)}R**", f(e["t_stat"], 1),
                f"{f(e['expectancy_before_costs_r'], 3, 'R', sign=True)}", f"{f(e['expectancy_pct'], 2, '%', sign=True)}",
                f"{f(e['spy_excess_pct'], 2, '%', sign=True)}", f(e["profit_factor"], 2),
                f"{f(e['hold'], 0)}", f"{f(100 * cf[f'{v}/{u}']['trades']['trades'] / e['trades'], 0)}%"])
        out += ["### Every signal", "",
                "Every signal taken, with no limit on positions or cash (each at a tiny size), so the statistics "
                "don't depend on the ranking or on which trades the portfolio had room for. Signals overlap in time "
                "(many fire in the same pullbacks of the market), so the standard errors and t-statistics overstate "
                "the precision.", "",
                table(RULE_HEAD + ["Trades (a year)", "Win rate", "Expectancy", "t", "Before costs", "Average trade",
                                   "vs SPY", "Profit factor", "Sessions held", "Share the portfolio took"], rows), ""]

        # -------------------------------------------------------------- volatility block
        for kind, title, spy_cagr in (("pre", "Before tax, against SPY before tax", sp["cagr_held"]),
                                      ("after", "After tax (each year's tax paid from the account), against SPY after tax",
                                       sa["cagr_held"])):
            cagr_key = "pre" if kind == "pre" else "after_held"
            rows = []
            for k in keys:
                b = cf[k][kind]
                rows.append(rule_cells(k.split("/")[0]) + [
                    f"{f(cf[k]['cagr'][cagr_key])}%", f"{f(b['vol'], 0)}%", f"{f(b['max_drawdown'], 0)}%",
                    f"{f(b['worst_year'])}% ({b['worst_year_label']})", f"{f(b['worst_12m'])}%",
                    f"{f(b['longest_underwater_months'], 0)} months", f"{f(b['ahead_3y'], 0)}%", f"{f(b['ahead_5y'], 0)}%",
                    f"{f(b['longest_behind_months'], 0)} months", f"{f(b['worst_12m_shortfall'])} pts"])
            b = sb[kind]
            rows.append(["SPY", "", "", f"{f(spy_cagr)}%", f"{f(b['vol'], 0)}%", f"{f(b['max_drawdown'], 0)}%",
                         f"{f(b['worst_year'])}% ({b['worst_year_label']})", f"{f(b['worst_12m'])}%",
                         f"{f(b['longest_underwater_months'], 0)} months", "", "", "", ""])
            out += [f"### Volatility and the time behind SPY: {title.split(',')[0].lower()}" if kind == "after" else
                    "### Volatility and the time behind SPY", "", f"{title}.", "",
                    table(RULE_HEAD + ["CAGR", "Volatility", "Max drawdown", "Worst calendar year", "Worst 12 months",
                                       "Longest under a previous high", "3-year windows ahead of SPY",
                                       "5-year windows ahead of SPY", "Longest stretch behind SPY",
                                       "Worst 12-month shortfall"], rows), ""]
        out += ["Rolling windows step by month. *Behind SPY* follows the ratio of the two accounts: the longest time "
                "it stayed below its previous high.", ""]

        # -------------------------------------------------------------- taxes
        rows = []
        for k in keys:
            t, val = cf[k]["tax"], cf[k]["values"]
            rows.append(rule_cells(k.split("/")[0]) + [
                f"{f(t['tax_per_year_pct'], 2)}%", f"${t['taxes_total']:,.0f}",
                f"{f(t['st_share_of_gains'], 0)}%" if t["st_share_of_gains"] is not None else "–",
                f"{t['wash_sales']} (${t['wash_disallowed']:,.0f})", f"${t['carry_st']:,.0f} / ${t['carry_lt']:,.0f}",
                f"${t['dividends']:,.0f}", f"${val['pre']['held']:,.0f}",
                f"${val['after']['held']:,.0f} / ${val['after']['sold']:,.0f}"])
        sv = spy["combined/after"]
        rows.append(["SPY", "", "", f"{f(res['spy_tax_per_year_pct'], 2)}%", f"${sv['taxes_total']:,.0f}", "–", "0",
                     "–", f"${sv['stats'].get('dividends', 0):,.0f}", f"${spy['combined/pre']['values']['held']:,.0f}",
                     f"${sv['values']['held']:,.0f} / ${sv['values']['sold']:,.0f}"])
        out += ["### Taxes", "",
                table(RULE_HEAD + ["Tax paid a year (average, % of the account)", "Taxes paid in total",
                                   "Short-term share of realized gains", "Wash sales (losses deferred)",
                                   "Loss carryforward left: short / long term", "Dividends received",
                                   "Ending value before tax", "Ending value after tax: held / sold"], rows), "",
                "From $100,000. A loss carryforward is worth something only to a reader with gains to offset.", ""]

        # -------------------------------------------------------------- leverage check
        rows = []
        for k in keys:
            lc = cf[k]["leverage_check"]
            rows.append(rule_cells(k.split("/")[0]) + [
                f"{f(lc['leverage'], 2)}x", f"{f(lc['pre'])}%", f"{f(lc['after_held'])}% / {f(lc['after_sold'])}%",
                f"{f(lc['max_drawdown'], 0)}%", f"{f(cf[k]['cagr']['pre'])}% / {f(cf[k]['cagr']['after_held'])}%"])
        out += ["### The leverage check: SPY at the same volatility", "",
                "SPY held with margin, or partly in cash, so its volatility matches the strategy's, rebalanced monthly. "
                f"Margin at the 3-month T-bill rate plus {f(res['margin']['spread'], 1)}%, at least "
                f"{f(res['margin']['floor'], 0)}% ({f(res['margin']['today'], 1)}% today); idle cash earns nothing; "
                "the same tax rules. Below 1x means SPY partly in cash.", "",
                table(RULE_HEAD + ["SPY exposure", "SPY at that exposure: before tax", "After tax, held / sold",
                                   "Its max drawdown", "The strategy: before / after tax (held)"], rows), ""]

        # -------------------------------------------------------------- selection
        rows = []
        for k in keys:
            a, z = mc[f"{k}/pre"], mc[f"{k}/after"]
            rows.append(rule_cells(k.split("/")[0]) + [
                f"{f(cf[k]['cagr']['pre'])}% / {f(cf[k]['cagr']['after_held'])}%",
                f"{f(a['ranked_percentile'], 0)} / {f(z['ranked_percentile'], 0)}",
                f"{f(a['p5'])}% / **{f(a['p50'])}%** / {f(a['p95'])}%",
                f"{f(z['p5'])}% / **{f(z['p50'])}%** / {f(z['p95'])}%",
                f"{f(a['beat_spy'], 0)}% / {f(z['beat_spy'], 0)}%",
                f"{f(cf[k]['ret63']['pre'])}% / {f(cf[k]['ret63']['after_held'])}%"])
        out += ["### Selection: the ranking against random selection", "",
                f"{res['seeds']} random selections per row, combined period. Before tax / after tax (held).", "",
                table(RULE_HEAD + ["12-month momentum ranking", "Its percentile among the random runs",
                                   "Random: 5th / median / 95th, before tax", "Random: 5th / median / 95th, after tax",
                                   "Random runs beating SPY (before / after tax)", "3-month ranking"], rows), ""]

        # -------------------------------------------------------------- robustness
        rows = [["As run"] + [f"{f(cf[k]['cagr']['pre'])}% / {f(cf[k]['cagr']['after_held'])}%" for k in keys]]
        for name, pick in GENERIC:
            cells = []
            for k in keys:
                lab = pick(k.split("/")[0])
                r = rb[k].get(lab)
                cells.append(f"{f(r['pre'])}% / {f(r['after_held'])}%" if r else "–")
            if any(c != "–" for c in cells):
                rows.append([name] + cells)
        rows.append(["Five largest winners made nothing (before tax, approximate)"]
                    + [f"{f(cf[k]['without_top5'])}%" for k in keys])
        out += ["### Robustness", "",
                "Ranked by 12-month momentum, combined period; CAGR before tax / after tax (held). Columns: exit / "
                "stop / trend filter. The last row subtracts the five largest winners' dollar gains from the ending "
                "value, ignoring what that money compounded into afterwards.", "",
                table(["Check"] + [head(k.split("/")[0]) for k in keys], rows), ""]

        # -------------------------------------------------------------- periods
        rows = []
        for period in ("in_sample", "out_of_sample"):
            for k in keys:
                pr = cf[k]["periods"][period]
                rows.append([PERIOD_TITLES[period]] + rule_cells(k.split("/")[0]) + [
                    f"{f(pr['pre']['cagr'])}%", f"{f(pr['after_held'])}%", f"{f(pr['after_sold'])}%",
                    f"{f(pr['pre']['max_drawdown'], 0)}%",
                    f"{pm(pr['trades']['expectancy_r'], pr['trades']['expectancy_se'])}R", f"{pr['trades']['trades']:,}"])
            pr = cf[keys[0]]["periods"][period]
            rows.append([PERIOD_TITLES[period], "SPY", "", "", f"{f(pr['spy_pre'])}%", f"{f(pr['spy_after_held'])}%",
                         f"{f(pr['spy_after_sold'])}%", "", "", ""])
        out += ["### The two periods", "", "Each a separate run from $100,000.", "",
                table(["Period"] + RULE_HEAD + ["Before tax", "After tax, held", "After tax, sold", "Max drawdown",
                                                "Expectancy", "Trades"], rows), ""]

        # -------------------------------------------------------------- yearly
        years = list(cf[keys[0]]["pre"]["yearly"])
        rows = []
        for y in years:
            rows.append([y] + [f"{f(cf[k]['pre']['yearly'][y])}% / {f(cf[k]['after']['yearly'][y])}%" for k in keys]
                        + [f"{f(sb['pre']['yearly'][y])}% / {f(sb['after']['yearly'][y])}%"])
        out += ["### Yearly returns", "",
                "Before tax / after tax (a year's tax comes out at the start of the next year). Columns: exit / stop "
                "/ trend filter.", "",
                table(["Year"] + [head(k.split("/")[0]) for k in keys] + ["SPY"], rows), ""]

    # ------------------------------------------------------------------ method
    out += ["## Method and assumptions", "",
            "Rules (fixed before any results):",
            "- Indicators on split-adjusted daily bars: the 50- and 200-day EMAs of the close and the 20-day ATR "
            "(Wilder's smoothing). The area of value is the 50-day EMA ± 0.5 ATR.",
            "- Touch counting, one state machine per stock over the whole history: a pullback starts when the low "
            "reaches the area after the stock has traded clear above it (a low above the area); it is a completed "
            "test when a later high exceeds the swing high before it (the highest high since the stock last cleared "
            "the area); a close below the area resets the count to zero. Only the third pullback, with two completed "
            "tests before it, is traded.",
            "- Trigger: a hammer (close in the top quarter of the day's range, lower shadow at least twice the body) "
            "or a bullish engulfing pattern (a down candle, then an up candle whose body covers it), on a session "
            "that reaches the area or the one after such a session. The first trigger of the pullback is the signal; "
            "buy at the next open.",
            "- Trend filter at the signal: the close above the 200-day EMA; the *rising* version also needs the "
            "200-day EMA above its level 20 sessions earlier.",
            "- Stops, from the signal session's ATR: 1 ATR below the pullback's lowest low, or 2 ATR below the entry "
            "(the next open). A stop is an order: filled at the stop during the day, at the open on a gap through it.",
            "- Exits: a limit 0.25 ATR below the swing high before the pullback (\"just before the swing high\"; a "
            "trade whose entry is already at or above it is skipped), or the next open after a close below the "
            "50-day EMA (the stop stays as a hard stop). Stops take priority over targets within a day.",
            "- Universes: S&P 500 + MidCap 400 (the closest to the Russell 1000 his own stock systems use) or "
            "Russell 3000 members at the signal session (point in time: the S&P 500 change log, Wikipedia's S&P 400 "
            "list checked against IJH, archived iShares holdings for the Russell), as-traded close of $1 or more.",
            "- Size: 1% of equity at risk (shares = 1% of equity / (fill − stop)), at most 20% of equity per stock "
            "and 10 positions; positions shrink to the cash available (no margin).",
            "- Ranking when more stocks signal than slots and cash allow: 12-month momentum skipping the latest month "
            "(the return from 252 to 21 sessions before the signal), highest first, chosen before the run. Random "
            "selection is the baseline; the 3-month return is an alternative.",
            "- Costs: slippage 0.10% a side, 0.25% when the as-traded price is under $20. No commissions.", "",
            "Judgement calls where his rules are loose: the area's width (±0.5 ATR), what counts as a test (a new high "
            "after the touch) and as a failure (a close below the area), \"just before\" the swing high (0.25 ATR), "
            "the 20-session slope for \"pointing higher\", the 20-day ATR, and EMAs rather than simple averages (he "
            "uses EMAs). The robustness table moves each of these one notch.", "",
            "Dividends, taxes and benchmarks:",
            "- Dividends: included, credited at the ex-date to shares held at the previous close (split-adjusted).",
            "- Taxes: 40.8% on short-term gains and ordinary dividends, 23.8% on long-term gains and qualified "
            "dividends (top federal rates with the 3.8% net investment income tax); no state tax. Lots first in, "
            "first out; long-term after more than a year. Short- and long-term results net within the year; losses "
            "carry forward by class; no deduction against other income. Wash sales: a loss on shares bought back "
            "within 30 days is deferred into the new shares (clawed back if the loss was already used). Dividends are "
            "qualified when the shares were held more than 60 days of the 121 around the ex-date.",
            "- Payment: each year's tax comes out of the account at the first session of the next year, from cash "
            "first and then by selling every position pro rata at the open. Each period starts from $100,000 with no "
            "carryforward. Taxes never change a trade.",
            "- Ending value *held*: the tax on everything realized is paid; open positions are not sold. *Sold*: open "
            "positions are sold at the last close and taxed.",
            "- SPY: bought at the first close, dividends reinvested at the ex-date close, the same taxes paid by "
            "selling SPY.",
            "- Margin (the leverage check only; the strategy never borrows): the 3-month T-bill rate plus 5.5%, at "
            "least 8%; deductible against short-term gains and ordinary dividends, the rest carried forward.",
            "- Data limits: delisted stocks are thin before 2021, so 2016–2020 is flattered (more for the Russell "
            "3000). Russell membership comes from periodic snapshots: a stock counts when the latest snapshot of any "
            "of the three funds, or one from the previous 190 days, holds it. The $1 floor admits illiquid small caps "
            "in the Russell 3000, where the slippage assumed is probably too low.", "",
            "Sources (Rayner Teo, tradingwithrayner.com): \"A Moving Average Trading Strategy That Actually Works\" "
            "(50/200 MAs, the area of value, the higher close or a rejection candle, 1 ATR below the low, exits at "
            "the swing high or on a close below the 50 MA), \"Moving Average Trading Strategy (This Is No Longer a "
            "Secret)\" (a minimum of two tests, the next candle's open, 1 ATR beyond the swing), \"Moving Average "
            "Didn't Work till I Discovered This Secret\" (20/50/100–200 MAs by trend strength, entry on the third "
            "test, the 20-period ATR), \"The Trend Trading Strategy Guide\" (the 200 MA pointing higher, 2 ATR from "
            "entry, the nearest swing high), \"The Moving Average Indicator Guide\" (EMAs, the trailing exit) and "
            "\"The Complete Guide to Candlestick Patterns\" (the hammer and engulfing definitions).", "",
            "Files: `results.json`, `curves_combined.csv` (before- and after-tax curves), `montecarlo.csv`, "
            "`trades_*.csv` (the portfolio's trades before tax)."]
    return "\n".join(out) + "\n"
