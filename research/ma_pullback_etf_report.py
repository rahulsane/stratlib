"""The report for run_ma_pullback_etf.py (output/ma_pullback_etf/report.md), written from its results.json."""

from __future__ import annotations

import report_rules as rr
from ma_pullback_report import RULE_HEAD, head, pm, rng, rule_cells
from run_ma_pullback_etf import HORIZONS, PERIOD_TITLES, TOUCHES, VARIANTS
from run_wtt import f, table

TOUCH_SHORT = {"third": "Third only", "third+": "Third or later", "any": "Any"}
ROBUST = ["double costs", "entries a session late", "area 0.25 ATR either side", "area 0.75 ATR either side",
          "40-day EMA (area and exit)", "60-day EMA (area and exit)", "higher-close trigger",
          ("stop 0.5 ATR below the low", "stop 1.5 ATR below the entry"),
          ("stop 1.5 ATR below the low", "stop 2.5 ATR below the entry"),
          "target at the swing high", "target 0.5 ATR below it", "200-day EMA rising over 10 sessions",
          "200-day EMA rising over 40 sessions", "half the account per position"]
ROBUST_NAMES = ["Double costs", "Entries a session late", "Area 0.25 ATR either side", "Area 0.75 ATR either side",
                "40-day EMA (area and exit)", "60-day EMA (area and exit)", "Higher-close trigger",
                "Stop one notch tighter", "Stop one notch looser", "Target at the swing high",
                "Target 0.5 ATR below it", "200-day EMA rising over 10 sessions",
                "200-day EMA rising over 40 sessions", "Half the account per position (not 1% risk)"]


def keys() -> list[str]:
    return [f"{v}/{t}" for t in TOUCHES for v in VARIANTS]


def cells(k: str) -> list[str]:
    v, t = k.split("/")
    return [TOUCH_SHORT[t]] + rule_cells(v)


HEAD = ["Touch"] + RULE_HEAD


def findings(res: dict) -> str:
    cf, b, ev = res["configs"], res["bench"], res["events"]
    sp, sa = b["SPY/long/pre"], b["SPY/long/after"]
    ks = keys()
    third = [k for k in ks if k.endswith("/third")]
    anyk = [k for k in ks if k.endswith("/any")]
    n_third = cf[third[0]]["trades"]["trades"]
    by_sym = {s: cf[third[0]]["instruments"][s]["trades"]["trades"] for s in ("SPY", "QQQ")}
    lev = [cf[k]["leverage_check"] for k in ks]
    ev20 = {s: ev[f"any/above/{s}"]["20"] for s in ("SPY", "QQQ")}
    ev60 = {s: ev[f"any/above/{s}"]["60"] for s in ("SPY", "QQQ")}
    ev_third20 = {s: ev[f"third/above/{s}"]["20"] for s in ("SPY", "QQQ")}
    rb = [x["pre"] for k in res["robust"] for x in res["robust"][k].values()]
    half = [res["robust"][k]["half the account per position"]["pre"] for k in res["robust"]]
    inst = {s: [cf[k]["instruments"][s] for k in anyk] for s in ("SPY", "QQQ")}
    p2000 = cf[ks[0]]["periods"]["since_2000"]
    items = [
        f"**As specified, the rule hardly ever trades on the index ETFs.** The third pullback into the 50-day EMA "
        f"area, after two completed tests and with a hammer or engulfing trigger, came {n_third} times in 33 years: "
        f"{by_sym['SPY']} on SPY and {by_sym['QQQ']} on QQQ. Indexes rarely bounce off the area twice without "
        "closing below it, which resets the count. The account held a position in "
        f"{rng([cf[k]['time_in_market'] for k in third], 0)} of sessions, had {rng([cf[k]['invested'] for k in third])} "
        "of its value invested on average, and made "
        f"{rng([cf[k]['cagr']['pre'] for k in third], 2)} a year before tax, against SPY's {f(sp['cagr_held'])}% from "
        f"1994 ({f(sa['cagr_held'])}% after tax). Thirteen trades cannot show an edge either way: the expectancy was "
        f"{rng([cf[k]['trades']['expectancy_r'] for k in third], 2, 'R', True)} with standard errors near 0.2R.",
        f"**Loosening the touch count gives enough trades, and they show no edge.** Taking every pullback into the "
        f"area (a departure from the rule, added after counting) gave {rng([cf[k]['trades']['trades'] for k in anyk], 0, '')} "
        f"trades, an expectancy of {rng([cf[k]['trades']['expectancy_r'] for k in anyk], 2, 'R', True)} (± about "
        f"{f(min(cf[k]['trades']['expectancy_se'] for k in anyk), 2)}–{f(max(cf[k]['trades']['expectancy_se'] for k in anyk), 2)}R) "
        f"and {rng([cf[k]['cagr']['pre'] for k in anyk], 2)} a year, with "
        f"{rng([cf[k]['invested'] for k in anyk], 0)} invested on average. The target exit lost on balance "
        f"({rng([cf[k]['trades']['expectancy_r'] for k in anyk if k.startswith('target')], 2, 'R', True)}); the EMA "
        f"exit roughly broke even. The third-or-later rule ({rng([cf[k]['trades']['trades'] for k in ks if k.endswith('/third+')], 0, '')} "
        f"trades) lost {rng([-cf[k]['trades']['expectancy_r'] for k in ks if k.endswith('/third+')], 2, 'R')} a trade.",
        f"**Buying the bounce did no better than buying any day in an uptrend.** After the any-touch signals, the next "
        f"20 sessions returned {f(ev20['SPY']['mean'], 2, '%', True)} on SPY and {f(ev20['QQQ']['mean'], 2, '%', True)} on "
        f"QQQ, against {f(ev20['SPY']['base_mean'], 2, '%', True)} and {f(ev20['QQQ']['base_mean'], 2, '%', True)} after "
        f"an average session above the 200-day EMA (differences {f(ev20['SPY']['diff'], 2, sign=True)} and "
        f"{f(ev20['QQQ']['diff'], 2, sign=True)} points, t = {f(ev20['SPY']['t'], 1)} and {f(ev20['QQQ']['t'], 1)}). "
        f"Over 60 sessions the signals trailed by {f(-ev60['SPY']['diff'], 1)} and {f(-ev60['QQQ']['diff'], 1)} points. "
        f"The third-touch signals did the same or worse (20 sessions: {f(ev_third20['SPY']['diff'], 2, sign=True)} on "
        f"SPY over {ev_third20['SPY']['n']} signals, {f(ev_third20['QQQ']['diff'], 2, sign=True)} on QQQ over "
        f"{ev_third20['QQQ']['n']}).",
        f"**On SPY alone the bounce roughly broke even; on QQQ it lost.** With every touch counted, SPY's "
        f"{rng([x['trades']['trades'] for x in inst['SPY']], 0, '')} trades averaged "
        f"{rng([x['trades']['expectancy_r'] for x in inst['SPY']], 2, 'R', True)} (standard errors "
        f"{rng([x['trades']['expectancy_se'] for x in inst['SPY']], 2, 'R')}) and made "
        f"{rng([x['cagr'] for x in inst['SPY']], 2)} a year on their own. QQQ's "
        f"{rng([x['trades']['trades'] for x in inst['QQQ']], 0, '')} averaged "
        f"{rng([x['trades']['expectancy_r'] for x in inst['QQQ']], 2, 'R', True)} (± "
        f"{rng([x['trades']['expectancy_se'] for x in inst['QQQ']], 2, 'R')}) and made "
        f"{rng([x['cagr'] for x in inst['QQQ']], 2)}. From 2000, when both trade, SPY made {f(p2000['spy_pre'])}% a "
        f"year and QQQ {f(p2000['qqq_pre'])}%; the strategy made {rng([cf[k]['periods']['since_2000']['pre'] for k in ks], 2)}.",
        f"**Mostly in cash, so it is calm, but SPY at the same volatility made more.** Volatility was "
        f"{rng([cf[k]['pre']['vol'] for k in ks], 1)} against SPY's {f(res['spy_block']['pre']['vol'], 0)}%, and the "
        f"maximum drawdown {rng([cf[k]['pre']['max_drawdown'] for k in ks], 0)} (SPY {f(res['spy_block']['pre']['max_drawdown'], 0)}%). "
        f"SPY held at the same volatility ({f(min(x['leverage'] for x in lev), 2)}–{f(max(x['leverage'] for x in lev), 2)}x, "
        f"the rest in cash earning nothing) made {rng([x['pre'] for x in lev])} a year, more than every version. "
        f"Sizing each trade at half the account instead of 1% risk gave {rng(half, 2)} (any touch); the robustness "
        f"checks ranged from {f(min(rb), 2)}% to {f(max(rb), 2)}% a year.",
    ]
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))


def rules_section() -> list[str]:
    return rr.section(
        ["Rayner Teo's moving-average pullback, as fixed for the stock study, on daily bars, long only:", "",
         "1. Area of value: the 50-day EMA, plus or minus half a 20-day ATR.",
         "2. Touches: a pullback into the area is a completed test when the price then makes a new high above the swing "
         "high before it; a close below the area resets the count. As specified, only the third pullback after two "
         "completed tests is traded (*Third only*). *The touch rules* below adds two looser counts.",
         "3. Trigger: a hammer (close in the top quarter of the day's range, lower shadow at least twice the body) or a "
         "bullish engulfing candle (a down candle, then an up candle whose body covers it), on a session that reaches "
         "the area or the one after. Buy at the next open; one entry per pullback.",
         "4. Size: 1% of the account at risk per trade, no margin."],
        ["His articles disagree on three rules, so all eight combinations run. The tables name them:", "",
         "- Trend filter. *Above 200*: the close is above the 200-day EMA. *Rising 200*: also, the 200-day EMA is above "
         "its level 20 sessions earlier.",
         "- Stop. *Swing-low stop*: 1 ATR below the pullback's lowest low. *Entry stop*: 2 ATR below the entry price.",
         "- Exit. *Target*: a limit 0.25 ATR below the swing high before the pullback, with the stop left in place; a "
         "trade whose entry is already at or above it is skipped. *EMA exit*: sell at the next open after a close "
         "below the 50-day EMA, with the stop kept as a hard stop."],
        ["A stop fills at the stop price, or at the open on a gap through it, and comes before a target on the same "
         "day. The EMAs are of the split-adjusted close; the ATR uses Wilder's smoothing. *Method and assumptions* at "
         "the end covers data, sizing, costs and taxes; the stock study lists the judgement calls behind the rules and "
         "moves each one notch in its robustness checks."])


def report(res: dict) -> str:
    cf, b, ev, sb = res["configs"], res["bench"], res["events"], res["spy_block"]
    sp, sa = b["SPY/long/pre"], b["SPY/long/after"]
    ks = keys()
    out = ["# Rayner Teo's moving-average pullback on SPY and QQQ", "",
           f"Data through {res['data_through']}. The same rules and eight variants as the stock study, "
           "[*Rayner Teo's moving-average pullback, before and after tax*](/reports?report=ma-pullback-rayner-teo), applied to SPY and QQQ. Method at the end.", "", *rules_section(), "## Findings", "",
           findings(res), "", "![After-tax growth and drawdowns](equity_drawdown.png)", ""]

    # ------------------------------------------------------------------ touch rules
    third = cf[f"target-swing-above/third"]
    out += ["## The touch rules", "",
            "The stock study traded only the third touch, as fixed before it ran. On two ETFs that gives 13 signals in "
            "33 years, so two looser rules were added after counting, as departures that are reported apart:", "",
            table(["Touch rule", "What is traded", "Signals on SPY", "Signals on QQQ"], [
                [TOUCHES[t][1], {"third": "only the third pullback after two completed tests",
                                 "third+": "the third or any later pullback (\"a minimum of two tests\")",
                                 "any": "every pullback into the area (no test count)"}[t],
                 str(ev[f"{t}/above/SPY"]["5"]["n"]), str(ev[f"{t}/above/QQQ"]["5"]["n"])] for t in TOUCHES]), "",
            "Signals with the price-above-200 filter, the first trigger of each pullback, 1994 on (QQQ from 2000). "
            "The rising-200 filter removes at most one. A signal is not traded while the same ETF is still held, so "
            "the trade counts below can be lower.", ""]
    del third

    # ------------------------------------------------------------------ headline
    rows = [cells(k) + [f"{cf[k]['trades']['trades']}", f"{f(cf[k]['time_in_market'], 0)}% / {f(cf[k]['invested'], 1)}%",
                        f"**{f(cf[k]['cagr']['pre'], 2)}%**", f"{f(cf[k]['cagr']['after_held'], 2)}%",
                        f"{f(cf[k]['cagr']['after_sold'], 2)}%", f"{f(cf[k]['pre']['vol'], 1)}%",
                        f"{f(cf[k]['pre']['max_drawdown'], 0)}%", f"{f(cf[k]['periods']['combined']['pre'], 2)}%"]
            for k in ks]
    pc = cf[ks[0]]["periods"]["combined"]
    rows.append(["SPY", "", "", "", "", "100% / 100%", f"**{f(sp['cagr_held'])}%**", f"{f(sa['cagr_held'])}%",
                 f"{f(sa['cagr_sold'])}%", f"{f(sb['pre']['vol'], 1)}%", f"{f(sb['pre']['max_drawdown'], 0)}%",
                 f"{f(pc['spy_pre'])}%"])
    rows.append(["QQQ (from 2000)", "", "", "", "", "", f"**{f(cf[ks[0]]['periods']['since_2000']['qqq_pre'])}%**",
                 f"{f(cf[ks[0]]['periods']['since_2000']['qqq_after_held'])}%", "", "", "", f"{f(pc['qqq_pre'])}%"])
    out += ["## Before and after tax", "",
            "1994 to the last close, from $100,000. QQQ trades from 2000, once its averages have formed. *Time in "
            "market*: sessions with a position; *invested*: the average share of the account in the ETFs. *Held*: "
            "taxes paid on everything realized; *sold*: everything sold at the end and taxed. Volatility and drawdown "
            "before tax. The last column is a separate run from 2016, the window the stock study uses.", "",
            table(HEAD + ["Trades", "Time in market / invested", "Before tax", "After tax, held", "After tax, sold",
                          "Volatility", "Max drawdown", "2016 on, before tax"], rows), ""]

    # ------------------------------------------------------------------ trades
    rows = []
    for k in ks:
        t = cf[k]["trades"]
        rows.append(cells(k) + [
            f"{t['trades']}", f"{f(t['win_rate'], 0)}%", f"{f(t['avg_win_r'], 2, 'R')} / {f(t['avg_loss_r'], 2, 'R')}",
            f(t["payoff"], 2), f"**{pm(t['expectancy_r'], t['expectancy_se'])}R**",
            f"{f(t['expectancy_before_costs_r'], 2, 'R', sign=True)}", f"{f(t['expectancy_pct'], 2, '%', sign=True)}",
            f(t["profit_factor"], 2), f(t["sqn"], 2), f"{f(t['kelly'], 0, '%')}",
            f"{f(t['hold_win'], 0)} / {f(t['hold_loss'], 0)}", str(t["max_losing_streak"]),
            f"{f(t['losers_up_1r'], 0)}%"])
    out += ["## Trade statistics", "",
            "Every trade, 1994 on, before tax. Definitions as in the stock study: R is the distance from the entry fill "
            "to the initial stop; expectancy ± one standard error; SQN = expectancy / standard deviation × √(trades, at "
            "most 100); Kelly = win rate − loss rate / payoff.", "",
            table(HEAD + ["Trades", "Win rate", "Average win / loss", "Payoff", "Expectancy", "Before costs",
                          "Average trade", "Profit factor", "SQN", "Kelly", "Sessions held: winners / losers",
                          "Longest losing streak", "Losers that were 1R ahead"], rows), ""]

    rows = []
    for k in ks:
        row = cells(k)
        for s in ("SPY", "QQQ"):
            x = cf[k]["instruments"][s]
            t = x["trades"]
            row += [str(t["trades"]),
                    f"{pm(t['expectancy_r'], t['expectancy_se'])}R" if t["trades"] > 1 else "–", f"{f(x['cagr'], 2)}%"]
        rows.append(row)
    out += ["### Each ETF alone", "", "Separate runs, 1994 on (QQQ from 2000), before tax.", "",
            table(HEAD + ["SPY trades", "SPY expectancy", "SPY-only CAGR", "QQQ trades", "QQQ expectancy",
                          "QQQ-only CAGR"], rows), ""]

    # ------------------------------------------------------------------ events
    rows = []
    for t in TOUCHES:
        for s in ("SPY", "QQQ"):
            e = ev[f"{t}/above/{s}"]
            row = [TOUCHES[t][1], s, str(e[str(HORIZONS[0])]["n"])]
            for h in HORIZONS:
                x = e[str(h)]
                row.append(f"{f(x['mean'], 2, '%', True)} vs {f(x['base_mean'], 2, '%', True)} (t {f(x['t'], 1)})"
                           if "t" in x else "–")
            rows.append(row)
    out += ["## Event study: the bounce against an ordinary uptrend day", "",
            "The average return from the next open to the close 5, 10, 20 and 60 sessions later: after each signal "
            "(the first trigger of a pullback, price above the 200-day EMA) against after every session with the price "
            "above the 200-day EMA, 1994 on. t: the difference over its standard error; signals close together "
            "overlap, so it overstates the precision. The rising-200 filter gives the same figures within 0.2 points.",
            "", table(["Touch rule", "ETF", "Signals"] + [f"{h} sessions: signal vs uptrend day" for h in HORIZONS],
                      rows), ""]

    # ------------------------------------------------------------------ volatility block
    for kind, title in (("pre", "Before tax, against SPY before tax"),
                        ("after", "After tax (each year's tax paid from the account), against SPY after tax")):
            cagr_key = "pre" if kind == "pre" else "after_held"
            rows = []
            for k in ks:
                x = cf[k][kind]
                rows.append(cells(k) + [
                    f"{f(cf[k]['cagr'][cagr_key], 2)}%", f"{f(x['vol'], 1)}%", f"{f(x['max_drawdown'], 0)}%",
                    f"{f(x['worst_year'])}% ({x['worst_year_label']})", f"{f(x['worst_12m'])}%",
                    f"{f(x['longest_underwater_months'], 0)} months", f"{f(x['ahead_3y'], 0)}%", f"{f(x['ahead_5y'], 0)}%",
                    f"{f(x['longest_behind_months'], 0)} months", f"{f(x['worst_12m_shortfall'])} pts"])
            x = sb[kind]
            rows.append(["SPY", "", "", "", f"{f((sp if kind == 'pre' else sa)['cagr_held'], 2)}%", f"{f(x['vol'], 1)}%",
                         f"{f(x['max_drawdown'], 0)}%", f"{f(x['worst_year'])}% ({x['worst_year_label']})",
                         f"{f(x['worst_12m'])}%", f"{f(x['longest_underwater_months'], 0)} months", "", "", "", ""])
            out += ["## Volatility and the time behind SPY" if kind == "pre" else
                    "### After tax", "", f"{title}, 1994 on.", "",
                    table(HEAD + ["CAGR", "Volatility", "Max drawdown", "Worst calendar year", "Worst 12 months",
                                  "Longest under a previous high", "3-year windows ahead of SPY",
                                  "5-year windows ahead of SPY", "Longest stretch behind SPY",
                                  "Worst 12-month shortfall"], rows), ""]
    out += ["Rolling windows step by month. *Behind SPY* follows the ratio of the two accounts.", ""]

    # ------------------------------------------------------------------ taxes and leverage
    rows = []
    for k in ks:
        t, val = cf[k]["tax"], cf[k]["values"]
        rows.append(cells(k) + [
            f"{f(t['tax_per_year_pct'], 2)}%", f"${t['taxes_total']:,.0f}",
            f"{f(t['st_share_of_gains'], 0)}%" if t["st_share_of_gains"] is not None else "–",
            f"{t['wash_sales']} (${t['wash_disallowed']:,.0f})", f"${t['carry_st']:,.0f} / ${t['carry_lt']:,.0f}",
            f"${t['dividends']:,.0f}", f"${val['pre']['held']:,.0f}",
            f"${val['after']['held']:,.0f} / ${val['after']['sold']:,.0f}"])
    rows.append(["SPY", "", "", "", f"{f(sa['tax_per_year_pct'], 2)}%", f"${sa['taxes_total']:,.0f}", "–", "0", "–",
                 f"${sa['stats'].get('dividends', 0):,.0f}", f"${sp['values']['held']:,.0f}",
                 f"${sa['values']['held']:,.0f} / ${sa['values']['sold']:,.0f}"])
    out += ["## Taxes", "",
            table(HEAD + ["Tax paid a year (average, % of the account)", "Taxes paid in total",
                          "Short-term share of realized gains", "Wash sales (losses deferred)",
                          "Loss carryforward left: short / long term", "Dividends received", "Ending value before tax",
                          "Ending value after tax: held / sold"], rows), "",
            "From $100,000 in 1994. Wash sales can occur because the same two ETFs are bought again soon after a losing sale.", ""]
    rows = []
    for k in ks:
        lc = cf[k]["leverage_check"]
        rows.append(cells(k) + [f"{f(lc['leverage'], 2)}x", f"{f(lc['pre'], 2)}%",
                                f"{f(lc['after_held'], 2)}% / {f(lc['after_sold'], 2)}%", f"{f(lc['max_drawdown'], 0)}%",
                                f"{f(cf[k]['cagr']['pre'], 2)}% / {f(cf[k]['cagr']['after_held'], 2)}%"])
    out += ["## The leverage check: SPY at the same volatility", "",
            "SPY held partly in cash (idle cash earns nothing) so its volatility matches the strategy's, rebalanced "
            "monthly; the same taxes.", "",
            table(HEAD + ["SPY exposure", "SPY at that exposure: before tax", "After tax, held / sold",
                          "Its max drawdown", "The strategy: before / after tax (held)"], rows), ""]

    # ------------------------------------------------------------------ robustness
    anyk = [k for k in ks if k.endswith("/any")]
    rows = [["As run"] + [f"{f(cf[k]['cagr']['pre'], 2)}% / {f(cf[k]['cagr']['after_held'], 2)}% ({cf[k]['trades']['trades']})"
                          for k in anyk]]
    for name, lab in zip(ROBUST_NAMES, ROBUST):
        row = [name]
        for k in anyk:
            labs = lab if isinstance(lab, tuple) else (lab,)
            x = next((res["robust"][k][a] for a in labs if a in res["robust"][k]), None)
            row.append(f"{f(x['pre'], 2)}% / {f(x['after_held'], 2)}% ({x['trades']})" if x else "–")
        if any(c != "–" for c in row[1:]):
            rows.append(row)
    rows.append(["Five largest winners made nothing (before tax, approximate)"]
                + [f"{f(cf[k]['without_top5'], 2)}%" for k in anyk])
    out += ["## Robustness (any touch)", "",
            "The any-touch rule, the only one with enough trades to move; 1994 on; CAGR before tax / after tax (held) "
            "and the number of trades. Columns: exit / stop / trend filter.", "",
            table(["Check"] + [head(k.split("/")[0]) for k in anyk], rows), ""]

    # ------------------------------------------------------------------ periods
    rows = []
    for period in ("early", "combined", "since_2000"):
        for k in ks:
            x = cf[k]["periods"][period]
            rows.append([PERIOD_TITLES[period]] + cells(k) + [
                f"{f(x['pre'], 2)}%", f"{f(x['after_held'], 2)}%", f"{f(x['max_drawdown'], 0)}%",
                str(x["trades"]["trades"]),
                f"{pm(x['trades']['expectancy_r'], x['trades']['expectancy_se'])}R" if x["trades"]["trades"] > 1 else "–"])
        x = cf[ks[0]]["periods"][period]
        rows.append([PERIOD_TITLES[period], "SPY", "", "", "", f"{f(x['spy_pre'])}%", f"{f(x['spy_after_held'])}%", "", "", ""])
        if "qqq_pre" in x:
            rows.append([PERIOD_TITLES[period], "QQQ", "", "", "", f"{f(x['qqq_pre'])}%", f"{f(x['qqq_after_held'])}%",
                         "", "", ""])
    out += ["## Other periods", "", "Each a separate run from $100,000.", "",
            table(["Period"] + HEAD + ["Before tax", "After tax, held", "Max drawdown", "Trades", "Expectancy"], rows), ""]

    # ------------------------------------------------------------------ method
    out += ["## Method and assumptions", "",
            "- Rules, variants, trigger, stops and exits: as in the stock study (its *The rules tested* and *Method and "
            "assumptions*), applied to SPY and QQQ, and summarized in *Rules and assumptions* above.",
            "- Data: split-adjusted daily bars from each ETF's first trading day (SPY 29 January 1993, QQQ 10 March "
            "1999), dividends by ex-date, 3-month T-bill yields from 1990. The averages need about a year of bars, so "
            "the test starts in January 1994 and QQQ can first signal in early 2000.",
            "- Size: 1% of equity at risk per trade, Rayner's rule. There is no per-position cap other than the cash "
            "available (two broad index ETFs, at most two positions), and no margin. With stops one or two percent "
            "below the entry, a trade usually takes a third to two thirds of the account.",
            "- Costs: slippage 0.10% a side (both ETFs always traded above $20). No commissions.",
            "- Taxes and benchmarks: as in the stock study (top federal rates, taxes paid from the account at the start "
            "of the next year, wash sales, carryforwards, SPY taxed the same way, SPY at the same volatility). QQQ "
            "buy-and-hold is shown from 2000 for context.",
            "- Random selection, the stock study's baseline for its ranking, does not apply: two ETFs rarely signal "
            "together.",
            "- The two looser touch rules were added after seeing the signal count for the rule as specified; they are "
            "departures from the rule fixed for the stock study.", "",
            "Files: `results.json`, `curves_long.csv` (before- and after-tax curves), `trades_*.csv` (every trade, before "
            "tax)."]
    return "\n".join(out) + "\n"
