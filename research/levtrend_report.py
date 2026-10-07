"""The report for run_levtrend.py (output/levtrend/report.md), written from its results.json.

Audience: a reader who knows finance (CAGR, max drawdown, daily-reset leveraged ETFs, margin, wash sales) but not this
project, so standard terms stay as they are and only the project's own choices are explained.
"""

from __future__ import annotations

import report_rules as rr
from run_wtt import f, table

MAIN = "2x, month-end check"

# Display names for the versions, by their keys in results.json.
NAMES = {
    "SPY": "SPY buy-and-hold",
    "2x, month-end check": "2x, monthly signal",
    "2x, daily check": "2x, daily signal",
    "3x, month-end check": "3x, monthly signal",
    "75/25 mix, month-end check": "75/25 2x/3x, monthly signal",
    "2x through margin, month-end check": "2x on margin, monthly signal",
}
FIRST_TEST = {
    "1x hold": "S&P 500 buy-and-hold", "1x monthly": "1x, monthly signal", "1x daily": "1x, daily signal",
    "2x hold": "2x, no timing", "2x monthly": "**2x, monthly signal (main)**", "2x daily": "2x, daily signal",
    "3x hold": "3x, no timing", "3x monthly": "3x, monthly signal", "3x daily": "3x, daily signal",
}
CHANGES = {
    "200-day average, next-close fill (base case)": "Base case: 200-day SMA, fill at the next close",
    "150-day average": "150-day SMA",
    "250-day average": "250-day SMA",
    "fill at the signal day's close": "Fill at the signal close",
    "fill two closes later": "Fill one day late",
    "switch cost 0.25%": "Switch cost 0.25%",
    "financing spread 1.0%": "Financing spread 1.0%",
}


def money(x: float | None) -> str:
    if x is None:
        return "–"
    if abs(x) >= 1e9:
        return f"${x / 1e9:,.1f}B"
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.2f}M" if abs(x) < 1e7 else f"${x / 1e6:,.1f}M"
    return f"${x / 1e3:,.0f}k"


def pct(x, digits=1, sign=False):
    return f(x, digits, "%", sign)


def date_text(d: str) -> str:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
    return f"{int(d[8:])} {months[int(d[5:7]) - 1]} {d[:4]}"


def rolling_name(key: str) -> str:
    if key == "SPY buy and hold":
        return "SPY buy-and-hold"
    if key.startswith("mix"):
        return "75/25 2x/3x, monthly signal" + ("" if key.endswith("200-day") else f", {key.split()[-1][:3]}-day SMA")
    lev, check, n = key.split()
    name = f"{lev}, {check} signal"
    return name if n == "200-day" else f"{name}, {n[:3]}-day SMA"


def rules_section(res: dict) -> list[str]:
    fits, c = res["fits"], res["costs"]
    nf = res["nasdaq"]["fits"]
    return rr.section(
        ["### Strategy", "",
         "- Signal: the S&P 500's close against its 200-day simple moving average, checked at each month-end close.",
         "- Above the average: hold a 2x daily-reset S&P 500 ETF (SSO) for the month. Below: 3-month T-bills.",
         "- Trades fill at the next session's close; nothing happens between month-ends.",
         "",
         "Variations:",
         "",
         "- Daily signal: the same test at every close.",
         "- 1x and 3x (UPRO) instead of 2x; *no timing* holds the leverage throughout.",
         "- 150- and 250-day SMAs instead of 200.",
         "- 75/25 2x/3x: both legs on the monthly signal, rebalanced to 75/25 at each year-end."],
        ["### Pass criteria, set before the backtest", "",
         "The rule, the main version (2x, monthly signal) and three pass criteria over 1950–2026 were fixed before any "
         "results were seen, so the result can't be a pick of the best-looking variant: CAGR above buy-and-hold, a "
         "higher CAGR in at least 5 of the 8 decades, and a max drawdown no worse than buy-and-hold's. The 30-year "
         "windows, the blend, taxes and the Nasdaq-100 test were follow-ups."],
        ["### Leverage and costs", "",
         "- Leverage resets daily, as in the ETFs: each session returns L × the index's total return − (L − 1) × "
         "(T-bill rate + financing spread) − the expense ratio, accrued by calendar days. Volatility decay is "
         "therefore included.",
         f"- The pre-specified test assumed a {c['spread']:.1f}% financing spread and a {c['fee_lev']:.1f}% expense "
         f"ratio: {c['spread'] + c['fee_lev']:.1f}% a year all-in for 2x, {2 * c['spread'] + c['fee_lev']:.1f}% for "
         f"3x. Fitting the model to the ETFs' actual total returns since launch gives all-in costs of "
         f"{fits['SSO']['all_in']:.1f}% for SSO and {fits['UPRO']['all_in']:.1f}% for UPRO; every later section "
         "uses those.",
         f"- Each switch between the ETF and T-bills costs {100 * c['switch']:.1f}% of equity. Cash earns the "
         "T-bill rate.",
         "- SSO launched in 2006 and UPRO in 2009; earlier years are synthetic."],
        ["### Data", "",
         "- S&P 500 daily closes from 1949, with Robert Shiller's monthly dividends spread over each month's sessions. "
         "Cash: Shiller's one-year rate for 1950–1989, the 3-month T-bill yield from 1990.",
         f"- Before SPY's 1993 launch, \"SPY\" is the index's total return less a {c['fee_1x']:.2f}% expense ratio; "
         "it tracks SPY's actual return within 0.06 points a year since 1993. Sections marked *since 1995* or "
         "*since 2016* use SPY's actual prices and dividends.",
         f"- Data through {date_text(res['data_through'])}."],
        ["### Taxes", "",
         "Results are pre-tax unless marked. After-tax results assume a taxable account at today's top federal rates "
         "in every year: 40.8% short-term and ordinary income (including T-bill interest), 23.8% long-term and "
         "qualified dividends, no state tax. FIFO lots, loss carryforwards, the wash-sale rule; each year's tax is "
         "paid from the account in January. *Held* leaves unrealized gains untaxed at the end; *liquidated* sells "
         "everything at the last close. The ETFs are assumed to make no distributions (they make small ones)."],
        ["### Comparisons", "",
         f"- 2x on margin: SPY at 2x through broker margin instead of a leveraged ETF, for accounts that can't hold "
         f"leveraged ETFs. Margin at the T-bill rate + {c['margin_spread']:.1f}%, floored at "
         f"{c['margin_floor']:.0f}%; rebalanced monthly; same signal.",
         "- Vol-matched SPY: SPY at constant leverage, set to match each version's volatility, with no timing; built "
         "from the same ETFs (SPY plus SSO, or SSO plus UPRO above 2x) and, separately, on margin. This isolates the "
         "timing rule from the leverage.",
         f"- Nasdaq-100: the same rules on QQQ, with QLD (2x) and TQQQ (3x) at fitted all-in costs of "
         f"{nf['QLD']['all_in']:.1f}% and {nf['TQQQ']['all_in']:.1f}%, signalled by QQQ's own moving average."],
    )


def findings(res: dict) -> str:
    reg, cr, rb, roll = res["main"], res["crash_1987"], res["robustness"], res["rolling"]
    m2, d2, h1, h2, m1 = reg["2x monthly"], reg["2x daily"], reg["1x hold"], reg["2x hold"], reg["1x monthly"]
    full, s95, s16 = (res["standard"][p]["strategies"] for p in ("full", "1995", "2016"))
    main, spy = full[MAIN], full["SPY"]
    blend, marg = main["same_vol_funds"], main["same_vol"]
    borrow = full["2x through margin, month-end check"]
    r200, r150, rspy = roll["2x monthly 200-day"], roll["2x monthly 150-day"], roll["SPY buy and hold"]
    r3, mix = roll["3x monthly 200-day"], roll["mix rebalanced monthly 200-day"]
    falls = [rb["daily"][k]["maxdd"] for k in ("150-day average", "250-day average", "fill two closes later")]
    mvals = [v["cagr"] for v in rb["monthly"].values()]
    na, nr = res["nasdaq"]["part_a"], res["nasdaq"]["rolling"]["20"]["rows"]
    qld, sso = na["QLD 2x, monthly 200-day"], na["S&P 2x (SSO), monthly 200-day"]
    after_tax = ("vol-matched SPY comes out slightly ahead" if blend["cagr_sold"] > main["cagr_held"] else
                 f"the strategy stays {f(main['cagr_held'] - blend['cagr_sold'], 1)} points ahead")
    items = [
        f"**2x on the monthly signal beat buy-and-hold by about {f(m2['cagr'] - h1['cagr'], 0)} points a year, "
        f"but failed the drawdown criterion.** Pre-specified test, 1950–2026: {pct(m2['cagr'])} CAGR against "
        f"{pct(h1['cagr'])}, ahead in {m2['decade_wins']} of 8 decades, but a {pct(m2['maxdd'])} max drawdown "
        f"against {pct(h1['maxdd'])}. It was fully invested into the 1987 crash ({pct(cr['crash_day_index_return'])} "
        "on 19 October) and only exited at the October month-end. Constant 2x without the filter returned "
        f"{pct(h2['cagr'])} with an {pct(h2['maxdd'], 0)} drawdown; the filter at 1x matched the index "
        f"({pct(m1['cagr'])}) with a {pct(m1['maxdd'], 0)} drawdown.",
        f"**The daily signal passed all three criteria, on timing luck.** {pct(d2['cagr'])} CAGR, "
        f"{pct(d2['maxdd'])} max drawdown, because it exited on 15 October 1987, two sessions before the crash. "
        f"With a 150- or 250-day SMA, or a one-day fill delay, max drawdown is about {pct(sum(falls) / len(falls), 0)}. "
        f"It switches about {f(d2['switches_per_year'], 0)} times a year and whipsawed to a "
        f"{pct(d2['worst_year'][0], 0)} year in {d2['worst_year'][1]}.",
        f"**The edge is robust in sign, not in size.** Single-parameter changes keep the monthly version between "
        f"{pct(min(mvals))} and {pct(max(mvals))} CAGR. Over all 560 rolling 30-year windows (1950–1996 starts, actual "
        f"ETF costs) it ended ahead of SPY in {pct(r200['beat_spy'], 0)}, with a median terminal value "
        f"{f(r200['median'] / rspy['median'], 1)}x SPY's. With a 150-day SMA: ahead in {pct(r150['beat_spy'], 0)}, "
        f"median {f(r150['median'] / rspy['median'], 1)}x. The 200- and 250-day SMAs are the best settings, so the "
        "headline is toward the favourable end.",
        f"**3x raises the median and the tail risk.** {pct(r3['cagr'])} CAGR from 1950 at UPRO's costs, but a "
        f"{pct(r3['maxdd'], 0)} drawdown in 1987, and its worst 30-year window ended below SPY's worst. The 75/25 "
        f"2x/3x blend sits between: {pct(mix['cagr'])} CAGR, {pct(mix['maxdd'], 0)} max drawdown, ahead of 2x alone "
        f"in {pct(mix['beat_2x'], 0)} of 30-year windows.",
        f"**After tax, the timing rule adds nothing over vol-matched leverage.** In a taxable account the monthly "
        f"version returned {pct(main['cagr_held'])} (held) against SPY's {pct(spy['cagr_held'])}, a "
        f"{f(main['cagr'] - main['cagr_held'], 0)}-point tax drag: every exit realizes gains, mostly long-term. "
        f"Vol-matched SPY ({f(blend['leverage'], 2)}x via the same ETFs, no timing) returned {pct(blend['cagr'])} "
        f"pre-tax, {f(main['cagr'] - blend['cagr'], 0)} points less than the strategy and with a deeper max drawdown "
        f"({pct(blend['max_drawdown'], 0)} against {pct(main['block']['max_drawdown'], 0)}), but "
        f"{pct(blend['cagr_held'])} held and {pct(blend['cagr_sold'])} liquidated after tax, since it defers its "
        f"gains; {after_tax}. In a tax-deferred account the pre-tax figures apply.",
        f"**Since 2016 it trails SPY after tax.** {pct(s16[MAIN]['cagr'])} pre-tax and {pct(s16[MAIN]['cagr_held'])} "
        f"after, against {pct(s16['SPY']['cagr'])} and {pct(s16['SPY']['cagr_held'])} for SPY, with a "
        f"{pct(s16[MAIN]['block']['max_drawdown'], 0)} drawdown in 2022. Since 1995 it leads after tax: "
        f"{pct(s95[MAIN]['cagr_held'])} against {pct(s95['SPY']['cagr_held'])}.",
        f"**On margin it doesn't work.** Financing 2x at retail margin rates instead of through SSO cut CAGR to "
        f"{pct(borrow['cagr'])} pre-tax and {pct(borrow['cagr_held'])} after tax, against SPY's {pct(spy['cagr'])} "
        f"and {pct(spy['cagr_held'])}. The ETFs finance near the T-bill rate; at margin rates even vol-matched SPY "
        f"({pct(marg['cagr'])}) underperforms unlevered SPY.",
        f"**On the Nasdaq-100 the filter protects much less.** From the March 2000 peak, QLD on the monthly signal "
        f"returned {pct(qld['cagr'])} CAGR with a {pct(qld['maxdd'], 0)} max drawdown, against {pct(sso['cagr'])} and "
        f"{pct(sso['maxdd'], 0)} for SSO. QQQ fell 36% from its March 2000 peak before the monthly signal turned. "
        f"In 20-year windows since 2000, QLD beat SSO in {pct(nr['QLD 2x, monthly 200-day']['beat_sp2x'], 0)}, and "
        f"its worst window ended at {money(nr['QLD 2x, monthly 200-day']['worst'])} against "
        f"{money(nr['S&P 2x (SSO), monthly 200-day']['worst'])} per $100,000.",
    ]
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))


def first_test_section(res: dict) -> list[str]:
    reg = res["main"]
    rows = []
    for key, label in FIRST_TEST.items():
        r = reg[key]
        rows.append([label, pct(r["cagr"]), f"{pct(r['maxdd'], 0)} ({r['dd_when'][1][:4]})", pct(r["vol"], 0),
                     f"{pct(r['worst_year'][0], 0)} ({r['worst_year'][1]})", f"{f(r['underwater_years'], 1)} years",
                     f(r["switches_per_year"], 1), pct(r["invested"], 0),
                     "–" if r["pass"] is None else f"{r['decade_wins']} of 8",
                     "–" if r["pass"] is None else ("yes" if r["pass"] else "no")])
    decs = list(reg["1x hold"]["decades"])
    drows = [[FIRST_TEST[k].replace("**", "")] + [pct(reg[k]["decades"][d]) for d in decs]
             for k in ("1x hold", "1x monthly", "2x monthly", "2x daily", "3x monthly", "2x hold")]
    return ["## Pre-specified test, 1950–2026", "",
            "Pre-specified costs, from the close on 3 January 1950. *Decades won* is against buy-and-hold (5 of 8 "
            "needed); *passed* means all three criteria.", "",
            table(["Version", "CAGR", "Max drawdown (trough)", "Volatility", "Worst year", "Longest underwater",
                   "Switches/yr", "Time invested", "Decades won", "Passed"], rows),
            "", "### CAGR by decade", "", "The 2020s run to September 2026.", "",
            table(["Version"] + decs, drows), ""]


def realism_section(res: dict) -> list[str]:
    v, fits = res["validation"], res["fits"]
    rows = [[f"SPY since {date_text(x['from'])}", pct(x["spy"], 1), pct(x["model"], 1)] for x in v["spy"]]
    for sym, x in v["funds"].items():
        rows.append([f"{sym} since {date_text(x['since'])}, pre-specified costs", pct(x["actual"], 1),
                     pct(x["model"], 1)])
    frows = [[f"{sym} ({fit['lev']}x)", pct(fit["fee"], 2), pct(fit["all_in"], 1), date_text(fit["start"]),
              pct(fit["actual_cagr"], 1)] for sym, fit in fits.items()]
    return ["## Model against the actual ETFs", "",
            table(["Series", "Actual CAGR", "Model CAGR"], rows), "",
            "All-in cost fitted to each ETF's total return since launch (3x ETFs from June 2009): the expense ratio plus "
            "financing above the T-bill rate. SSO's financing has run slightly below T-bills.", "",
            table(["ETF", "Expense ratio", "Fitted all-in cost", "Since", "Actual CAGR"], frows), ""]


def changes_section(res: dict) -> list[str]:
    out = ["## Sensitivity", "",
           "2x, one parameter changed at a time, 1950–2026, pre-specified costs. Buy-and-hold: "
           f"{pct(res['main']['1x hold']['cagr'])} CAGR, {pct(res['main']['1x hold']['maxdd'], 0)} max "
           "drawdown. A decade counts as won here only by 0.25 points or more.", ""]
    for check, label in (("monthly", "Monthly signal"), ("daily", "Daily signal")):
        rows = [[CHANGES[name], pct(x["cagr"]), pct(x["maxdd"], 0), f(x["switches_per_year"], 1),
                 f"{x['decades_ahead']} of 8", "yes" if x["pass"] else "no"]
                for name, x in res["robustness"][check].items()]
        out += [f"### {label}", "", table(["Change", "CAGR", "Max drawdown", "Switches/yr", "Decades won", "Passed"],
                                          rows), ""]
    return out


def modern_section(res: dict) -> list[str]:
    m = res["modern"]
    names = {"SPY buy and hold": "SPY buy-and-hold", "1x monthly": "1x, monthly signal", "1x daily": "1x, daily signal",
             "2x monthly": "2x, monthly signal", "2x daily": "2x, daily signal"}
    rows = [[label, pct(m[k]["cagr"]), pct(m[k]["maxdd"], 0), f"{pct(m[k]['worst_year'][0], 0)} ({m[k]['worst_year'][1]})",
             f(m[k]["switches_per_year"], 1) if "switches_per_year" in m[k] else "–"] for k, label in names.items()]
    return ["## SPY's actual returns, 1995–2026", "",
            "The same test on SPY's actual total return and T-bill yields from 3 January 1995, pre-specified costs.", "",
            table(["Version", "CAGR", "Max drawdown", "Worst year", "Switches/yr"], rows), ""]


def rolling_section(res: dict) -> list[str]:
    roll = res["rolling"]
    keys = ["SPY buy and hold", "2x monthly 150-day", "2x monthly 200-day", "2x monthly 250-day", "2x daily 150-day",
            "2x daily 200-day", "2x daily 250-day", "3x monthly 200-day", "3x daily 200-day",
            "mix rebalanced monthly 200-day"]
    rows, risk = [], []
    for k in keys:
        r = roll[k]
        rows.append([rolling_name(k), f"{money(r['worst'])} ({r['worst_start'][:4]})", money(r["p10"]),
                     money(r["median"]), money(r["p90"]), "–" if r["beat_spy"] is None else pct(r["beat_spy"], 0)])
        risk.append([rolling_name(k), pct(r["below5"], 0), pct(r["dd5_median"], 0), pct(r["dd5_p90"], 0),
                     pct(r["worst_day"], 0), f"{pct(r['worst_year'][0], 0)} ({r['worst_year'][1]})"])
    return ["## Rolling 30-year windows", "",
            "$100,000 invested on the first session of each month from February 1950 to September 1996 (560 windows), "
            "held 30 years, actual ETF costs, pre-tax. The windows overlap: roughly two and a half independent "
            "30-year periods.", "",
            table(["Version", "Worst (start)", "10th pct", "Median", "90th pct", "Ahead of SPY"], rows), "",
            "### Path risk", "",
            "Share of windows below the starting $100,000 after 5 years; max drawdown within the first 5 years (median "
            "and 90th percentile across windows); worst day and worst calendar year of the full 1950–2026 run.", "",
            table(["Version", "Below start after 5 yrs", "5-yr max drawdown, median", "5-yr max drawdown, 90th pct",
                   "Worst day", "Worst year"], risk), ""]


def bad_starts_section(res: dict) -> list[str]:
    ns = res["notable_starts"]
    labels = {"1966-01-03": "3 Jan 1966 (start of the 1966–82 flat market)",
              "1973-10-01": "1 Oct 1973 (before the 1973–74 bear)",
              "1987-10-01": "1 Oct 1987 (three weeks before the crash)", "2000-01-03": "3 Jan 2000 (dot-com peak)",
              "2007-10-01": "1 Oct 2007 (before the financial crisis)", "2022-01-03": "3 Jan 2022 (2022 bear)"}
    versions = {"SPY buy and hold": "SPY", "2x monthly 200-day": "2x monthly", "2x daily 200-day": "2x daily",
                "3x monthly 200-day": "3x monthly", "mix rebalanced monthly 200-day": "75/25 2x/3x"}
    rows = []
    for start, v in ns.items():
        last = "30y" if v["values"]["SPY buy and hold"]["30y"] is not None else "end"
        for key, short in versions.items():
            x = v["values"][key]
            rows.append([labels[start] if key == "SPY buy and hold" else "", short, money(x["1y"]), money(x["3y"]),
                         money(x["10y"]),
                         money(x[last]) + ("" if last == "30y" else f" ({f(v['years_to_end'], 0)} yrs)")])
    return ["## Selected entry points", "",
            "$100,000 at the start date's close, actual ETF costs, pre-tax. Last column: 30 years, or to September 2026.",
            "", table(["Start", "Version", "1 year", "3 years", "10 years", "30 years or to date"], rows), ""]


def tax_section(res: dict) -> list[str]:
    std = res["standard"]
    out = ["## After tax", "", "$100,000 at each period's first close, actual ETF costs. CAGR.", ""]
    titles = {"full": "1950–2026", "1995": "Since 1995 (SPY actual)", "2016": "Since 2016 (SPY actual)"}
    for period, p in std.items():
        rows = [[NAMES[n], pct(s["cagr"]), pct(s["cagr_held"]), pct(s["cagr_sold"])] for n, s in p["strategies"].items()]
        out += [f"### {titles[period]}", "", table(["Version", "Pre-tax", "After tax, held", "After tax, liquidated"],
                                                   rows), ""]
    full = std["full"]["strategies"]
    rows = [[NAMES[n], pct(s["tax"]["tax_per_year_pct"], 1),
             "–" if s["tax"]["st_share_of_gains"] is None else pct(s["tax"]["st_share_of_gains"], 0),
             str(s["tax"]["wash_sales"])] for n, s in full.items()]
    out += ["### Tax detail, 1950–2026", "",
            table(["Version", "Tax paid per year, % of equity", "Short-term share of realized gains", "Wash sales"], rows),
            ""]
    return out


def risk_section(res: dict) -> list[str]:
    std = res["standard"]
    out = ["## Risk against SPY", "",
           "Pre-tax, actual ETF costs. *Ahead in 5-yr windows*: share of rolling 5-year windows (month-end to month-end) "
           "with a higher return than SPY. *Longest behind SPY*: the longest drawdown of the strategy's value relative "
           "to SPY.", ""]
    titles = {"full": "1950–2026", "1995": "Since 1995", "2016": "Since 2016"}
    for period, p in std.items():
        rows = []
        for n, s in p["strategies"].items():
            b, spy = s["block"], n == "SPY"
            rows.append([NAMES[n], pct(b["vol"], 0), pct(b["max_drawdown"], 0),
                         f"{pct(b['worst_year'], 0)} ({b['worst_year_label']})", pct(b["worst_12m"], 0),
                         f"{f(b['longest_underwater_months'] / 12, 1)} yrs", "–" if spy else pct(b["ahead_5y"], 0),
                         "–" if spy else f"{f(b['longest_behind_months'] / 12, 1)} yrs",
                         "–" if spy else pct(b["worst_12m_shortfall"], 0, sign=True)])
        out += [f"### {titles[period]}", "",
                table(["Version", "Volatility", "Max drawdown", "Worst year", "Worst 12 months", "Longest underwater",
                       "Ahead in 5-yr windows", "Longest behind SPY", "Worst 12-month shortfall"], rows), ""]
    return out


def rule_or_leverage_section(res: dict) -> list[str]:
    std = res["standard"]
    out = ["## Timing rule or leverage?", "",
           "Each version against SPY at constant leverage matched to its volatility, with no timing: built from the same "
           "ETFs, and on margin. CAGR pre-tax / after tax (held). The ETF-built benchmark never trades, so its held "
           "figure carries no tax; its liquidated figure is in brackets.", ""]
    titles = {"full": "1950–2026", "1995": "Since 1995", "2016": "Since 2016"}
    for period, p in std.items():
        rows = []
        for n, s in p["strategies"].items():
            if n == "SPY":
                continue
            a, b = s["same_vol"], s["same_vol_funds"]
            rows.append([NAMES[n], f"{pct(s['cagr'])} / {pct(s['cagr_held'])}", pct(s["block"]["max_drawdown"], 0),
                         f"{f(b['leverage'], 2)}x", f"{pct(b['cagr'])} / {pct(b['cagr_held'])} ({pct(b['cagr_sold'])})",
                         pct(b["max_drawdown"], 0), f"{pct(a['cagr'])} / {pct(a['cagr_held'])}", pct(a["max_drawdown"], 0)])
        out += [f"### {titles[period]}", "",
                table(["Version", "Version CAGR", "Max drawdown", "Matching leverage", "Vol-matched SPY via ETFs",
                       "Max drawdown", "Vol-matched SPY on margin", "Max drawdown"], rows), ""]
    return out


def saver_section(res: dict) -> list[str]:
    d = res["dca"]
    names = {"SPY buy and hold": "SPY", "2x, month-end check": "2x, monthly signal", "2x, daily check": "2x, daily signal"}
    rows = [["Value, September 2026"] + [money(d["runs"][n]["final"]) for n in names],
            ["Multiple of contributions"] + [f"{f(d['runs'][n]['multiple'], 1)}x" for n in names],
            ["Money-weighted return"] + [pct(d["runs"][n]["mwr"]) for n in names],
            ["Largest drawdown"] + [f"{pct(d['runs'][n]['worst_fall']['pct'], 0)} ({d['runs'][n]['worst_fall']['from'][:4]}"
                                    f"–{d['runs'][n]['worst_fall']['to'][:4]})" for n in names]]
    mrows = [[date_text(m), money(d["paid_by"][m])] + [money(d["runs"][n]["marks"][m]) for n in names]
             for m in d["paid_by"]]
    return ["## Monthly contributions from 1995", "",
            f"$10,000 on 3 January 1995, then $500 at the first close of each month (${d['paid']:,.0f} contributed). "
            "SPY's actual returns, actual ETF costs, pre-tax.", "",
            table([""] + list(names.values()), rows), "", table(["Date", "Contributed"] + list(names.values()), mrows), ""]


def nasdaq_section(res: dict) -> list[str]:
    n = res["nasdaq"]
    pa, r20, b = n["part_a"], n["rolling"]["20"], n["part_b"]
    names = {"QQQ buy and hold": "QQQ buy-and-hold", "SPY buy and hold": "SPY buy-and-hold",
             "QLD 2x, monthly 200-day": "QLD (2x), monthly signal", "QLD 2x, monthly 250-day": "QLD, monthly, 250-day SMA",
             "QLD 2x, daily 200-day": "QLD, daily signal", "TQQQ 3x, monthly 200-day": "TQQQ (3x), monthly signal",
             "TQQQ 3x, daily 200-day": "TQQQ, daily signal", "S&P 2x (SSO), monthly 200-day": "SSO (2x S&P), monthly signal",
             "S&P 3x (UPRO), monthly 200-day": "UPRO (3x S&P), monthly signal"}
    rows = [[label, money(pa[k]["end"]), pct(pa[k]["cagr"]), pct(pa[k]["maxdd"], 0),
             f"{pct(pa[k]['worst_year'][0], 0)} ({pa[k]['worst_year'][1]})"] for k, label in names.items()]
    keys20 = ["QQQ buy and hold", "SPY buy and hold", "QLD 2x, monthly 200-day", "S&P 2x (SSO), monthly 200-day",
              "TQQQ 3x, monthly 200-day", "S&P 3x (UPRO), monthly 200-day"]
    twin = {"QLD 2x, monthly 200-day": "beat_sp2x", "TQQQ 3x, monthly 200-day": "beat_sp3x"}
    rows20 = [[names[k], money(r20["rows"][k]["worst"]), money(r20["rows"][k]["p10"]), money(r20["rows"][k]["median"]),
               pct(r20["rows"][k][twin[k]], 0) if k in twin else "–"] for k in keys20]
    bnames = {"Composite buy and hold": "Composite buy-and-hold", "2x, monthly 200-day": "2x, monthly signal",
              "2x, monthly 250-day": "2x, monthly, 250-day SMA", "2x, daily 200-day": "2x, daily signal",
              "3x, monthly 200-day": "3x, monthly signal"}
    brows = [[label, money(b["rows"][k]["worst"]), money(b["rows"][k]["median"]),
              "–" if k.startswith("Composite") else pct(b["rows"][k]["beat_hold"], 0),
              f"{pct(b['rows'][k]['maxdd'], 0)} ({b['rows'][k]['dd_year']})"] for k, label in bnames.items()]
    return ["## Nasdaq-100", "",
            f"QQQ, QLD and TQQQ from {date_text(n['start'])} (the first session with a 250-day history, which is the "
            "dot-com peak), $100,000 at the start, fitted ETF costs, pre-tax.", "",
            table(["Version", "Value, September 2026", "CAGR", "Max drawdown", "Worst year"], rows), "",
            "### Rolling 20-year windows since 2000", "",
            f"{r20['starts'][2]} monthly starts, {r20['starts'][0][:4]}–{r20['starts'][1][:4]}, $100,000 each. *Beat the "
            "S&P version*: share of windows in which the Nasdaq version ended above the S&P version at the same leverage.",
            "", table(["Version", "Worst", "10th pct", "Median", "Beat the S&P version"], rows20), "",
            "### Nasdaq Composite, 1971–2026", "",
            f"A longer check on the Composite, price-only for every version. 30-year windows starting "
            f"{b['starts'][0][:4]}–{b['starts'][1][:4]} ({b['starts'][2]} windows), $100,000 each, QLD's and TQQQ's "
            "fitted costs.", "",
            table(["Version", "Worst", "Median", "Ahead of buy-and-hold", "Max drawdown, 1972–2026"], brows), ""]


def report(res: dict) -> str:
    sig = res["signal"]
    out = ["# Leveraged S&P 500 with a 200-day trend filter", "",
           "A 2x leveraged S&P 500 ETF held while the index closes above its 200-day moving average, T-bills otherwise, "
           f"backtested against SPY buy-and-hold from 1950 to {date_text(res['data_through'])}.", "",
           *rules_section(res),
           "## Findings", "", findings(res), "",
           "![Growth of $100,000 and drawdowns, 1950–2026](equity_drawdown.png)", "",
           *first_test_section(res), *realism_section(res), *changes_section(res), *modern_section(res),
           *rolling_section(res), *bad_starts_section(res), *tax_section(res), *risk_section(res),
           *rule_or_leverage_section(res), *saver_section(res), *nasdaq_section(res),
           "## Caveats", "",
           "- One rule on one history: 1950–2026 holds about two and a half independent 30-year periods. The rule is "
           "well known (Gayed and Bilello, *Leverage for the Long Run*, 2016) and may be less effective from here.",
           "- The edge depends on the equity risk premium exceeding financing costs by a healthy margin; a decade of "
           "weaker equity returns would erode it and worsen the tails.",
           "- Close-to-close signals only. An intra-month crash hits the monthly version at full leverage, as in 1987. "
           "A one-day index decline of about 50% would wipe out a 2x ETF, about 33% a 3x ETF; market-wide circuit "
           "breakers now halt trading at a 20% decline.",
           "- Pre-2006 (2x) and pre-2009 (3x) results are synthetic; the ETFs may also close, change, or track "
           "differently in future.",
           "- After-tax results use today's top federal rates throughout, no state tax, and no other income to "
           "absorb losses.",
           f"- On {date_text(sig['day'])} the S&P 500 was {pct(sig['above_pct'], 1)} above its 200-day SMA, so the "
           "strategy was invested. This describes the backtest, not a recommendation.",
           "",
           "## Files", "",
           "- `results.json`: every figure in this report.",
           "- `curves_full.csv`: daily equity of $100,000 from 1950 for SPY, each version and vol-matched SPY; pre-tax, "
           "actual ETF costs.",
           "- `rolling_30y.csv`: terminal value of each of the 560 30-year windows, every version.",
           "- `switches.csv`: every signal change of the 2x monthly and daily versions since 1950, with the S&P 500 "
           "close and its 200-day SMA.",
           ""]
    return "\n".join(out)
