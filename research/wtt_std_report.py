"""The report for run_wtt_standard.py (output/wtt_std/report.md), written from its results.json."""

from __future__ import annotations

from engine import PERIOD_TITLES
from run_wtt import f, table

CONFIG_ORDER = ["wtt/r3000", "wtt/sp900", "qs/r3000", "qs/sp900"]
SHORT = {"wtt/r3000": "WTT, Russell 3000", "wtt/sp900": "WTT, S&P 900", "qs/r3000": "QS-style, Russell 3000",
         "qs/sp900": "QS-style, S&P 900"}
ROBUST_LABELS = ["double costs", "entries a week late", "breakout 15 weeks", "breakout 25 weeks",
                 "rate of change 25%", "rate of change 35%", "index average 8 weeks", "index average 12 weeks",
                 "wide stop 30%", "wide stop 50%", "tight stop 5%", "tight stop 15%"]


def findings(res: dict) -> str:
    cf, spy, mc, rb, sb = res["configs"], res["spy"], res["mc"], res["robust"], res["spy_block"]
    sp, sa = spy["combined/pre"], spy["combined/after"]
    c = {k: cf[k]["cagr"] for k in CONFIG_ORDER}
    wr, ws, qr, qs = (cf[k] for k in CONFIG_ORDER)

    def cells(key):
        return ", ".join(f"{SHORT[k]} {f(c[k][key])}%" for k in CONFIG_ORDER)

    lev = [cf[k]["leverage_check"] for k in CONFIG_ORDER]
    robust_range = {k: [rb[k][x]["pre"] for x in rb[k]] + [cf[k]["cagr"]["pre"]] for k in CONFIG_ORDER}
    qrp = qr["periods"]
    def span(key):
        vals = [c[k][key] for k in CONFIG_ORDER]
        return f"{f(min(vals))}% to {f(max(vals))}%"

    drags = [c[k]["pre"] - c[k]["after_held"] for k in CONFIG_ORDER]
    items = [
        f"**None of the four beat SPY, before or after tax.** Before tax they made {span('pre')} a year, against "
        f"SPY's {f(sp['cagr_held'])}% with dividends; after tax with positions held, {span('after_held')} against "
        f"{f(sa['cagr_held'])}%; sold at the end, {span('after_sold')} against {f(sa['cagr_sold'])}%. Of the "
        f"{res['seeds']} random selections behind each line, at most "
        f"{f(max(mc[f'{k}/after']['beat_spy'] for k in CONFIG_ORDER), 0)}% beat SPY after tax.",
        f"**Taxes cost far less than the gap to SPY.** The drag with positions held was {f(min(drags), 1)} to "
        f"{f(max(drags), 1)} points a year; SPY's own was {f(sp['cagr_held'] - sa['cagr_held'], 1)} points, or "
        f"{f(sp['cagr_held'] - sa['cagr_sold'], 1)} if sold at the end. WTT's quick exits make "
        f"{f(ws['tax']['st_share_of_gains'], 0)}% of its realized gains "
        f"short-term on the S&P 900; the QS-style version holds winners for years, so only "
        f"{f(qs['tax']['st_share_of_gains'], 0)}% are. WTT on the Russell 3000 lost money and finished with "
        f"${wr['tax']['carry_st']:,.0f} of losses to carry forward.",
        f"**The ranking chosen in advance failed on the Russell 3000.** Ranked by 12-month momentum, WTT lost "
        f"{f(-c['wtt/r3000']['pre'])}% a year, below all {res['seeds']} random selections (median "
        f"{f(mc['wtt/r3000/pre']['p50'])}%): it filled the slots with the most extended small caps and lost "
        f"{f(-wr['pre']['yearly']['2021'], 0)}% in 2021, {f(-wr['pre']['yearly']['2022'], 0)}% in 2022 and "
        f"{f(-wr['pre']['yearly']['2025'], 0)}% in 2025. On the S&P 900 the same ranking sat at the "
        f"{f(mc['wtt/sp900/pre']['ranked_percentile'], 0)}th percentile of the random runs. This repeats the "
        "earlier finding: among breakouts, a momentum ranking hurts in a broad universe and helps a little among "
        "index stocks.",
        f"**The closest to SPY used the other ranking, so treat it as hindsight.** The QS-style version on the S&P 900 "
        f"ranked by 3-month return made {f(qs['ret63']['pre'])}% before tax and {f(qs['ret63']['after_held'])}% after "
        f"(held), against SPY's {f(sp['cagr_held'])}% and {f(sa['cagr_held'])}%. It was not the rule chosen in advance, "
        "and with 10 positions a single path proves little.",
        f"**Small changes move the results a lot.** The QS-style Russell 3000 run made {f(c['qs/r3000']['pre'])}% a year, "
        f"yet {f(qrp['in_sample']['pre']['cagr'])}% and {f(qrp['out_of_sample']['pre']['cagr'])}% when 2016–2021 and "
        "2022 onward were run separately: the portfolio it carried into 2022 lost money, while a fresh start picked "
        f"other stocks. Its robustness checks ranged from {f(min(robust_range['qs/r3000']))}% to "
        f"{f(max(robust_range['qs/r3000']))}%; WTT on the S&P 900 from {f(min(robust_range['wtt/sp900']))}% to "
        f"{f(max(robust_range['wtt/sp900']))}%; the QS-style S&P 900 version was the steadiest "
        f"({f(min(robust_range['qs/sp900']))}% to {f(max(robust_range['qs/sp900']))}%).",
        f"**All four were more volatile than SPY or had deeper drawdowns, and spent most of the decade behind it.** "
        f"Volatility {f(min(cf[k]['pre']['vol'] for k in CONFIG_ORDER), 0)}–{f(max(cf[k]['pre']['vol'] for k in CONFIG_ORDER), 0)}% "
        f"against SPY's {f(sb['pre']['vol'], 0)}%; maximum drawdowns "
        f"{f(min(cf[k]['pre']['max_drawdown'] for k in CONFIG_ORDER), 0)}–{f(max(cf[k]['pre']['max_drawdown'] for k in CONFIG_ORDER), 0)}% "
        f"against {f(sb['pre']['max_drawdown'], 0)}%. WTT was ahead of SPY in no rolling 3- or 5-year window; the "
        f"QS-style S&P 900 version in {f(qs['pre']['ahead_3y'], 0)}% of 3-year windows and "
        f"{f(qs['pre']['ahead_5y'], 0)}% of 5-year ones.",
        f"**Taking the same risk through SPY on margin paid more.** Buying SPY on margin to match each strategy's "
        f"volatility ({f(min(x['leverage'] for x in lev), 2)}–{f(max(x['leverage'] for x in lev), 2)}x, margin "
        f"{f(res['margin']['floor'], 0)}–{f(res['margin']['max'], 1)}% over the decade) made "
        f"{f(min(x['pre'] for x in lev))}–{f(max(x['pre'] for x in lev))}% before tax and "
        f"{f(min(x['after_held'] for x in lev))}–{f(max(x['after_held'] for x in lev))}% after, more than every "
        f"strategy. After tax it only matched plain SPY ({f(sa['cagr_held'])}%): almost none of the interest was "
        "deductible (SPY held on margin realizes few short-term gains to set it against) and the monthly "
        "rebalancing realized long-term gains.",
    ]
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))


def report(res: dict) -> str:
    cf, spy, mc, rb, sb = res["configs"], res["spy"], res["mc"], res["robust"], res["spy_block"]
    title = {p: PERIOD_TITLES[p] for p in ("combined", "in_sample", "out_of_sample")}
    heads = [SHORT[c] for c in CONFIG_ORDER]
    sp, sa = spy["combined/pre"], spy["combined/after"]
    out = ["# WTT and the QuantifiedStrategies-style version under the reporting standard", "",
           f"Data through {res['data_through']}. Runner: `research/run_wtt_standard.py`; tax accounting: "
           "`research/tax_accounting.py`. Method and assumptions at the end.", "",
           "## Findings", "", findings(res), "", "![After-tax growth and drawdowns](equity_drawdown.png)", ""]

    rows = []
    for c in CONFIG_ORDER:
        x = cf[c]
        rows.append([SHORT[c], f"**{f(x['cagr']['pre'])}%**", f"{f(x['cagr']['after_held'])}%",
                     f"{f(x['cagr']['after_sold'])}%", f(x['cagr']['pre'] - x['cagr']['after_held'], 1, " pts"),
                     f"{f(x['pre']['vol'], 0)}%", f"{f(x['pre']['max_drawdown'], 0)}%"])
    rows.append(["SPY", f"**{f(sp['cagr_held'])}%**", f"{f(sa['cagr_held'])}%", f"{f(sa['cagr_sold'])}%",
                 f(sp['cagr_held'] - sa['cagr_held'], 1, " pts"), f"{f(sb['pre']['vol'], 0)}%",
                 f"{f(sb['pre']['max_drawdown'], 0)}%"])
    out += ["## Before and after tax", "",
            "Combined period (2016 to the last close), ranked by 12-month momentum, the rule chosen before the run. "
            "*Held*: taxes paid on everything realized; positions still open at the end are not sold. *Sold*: "
            "everything is sold at the end and taxed.", "",
            table(["", "Before tax", "After tax, held", "After tax, sold", "Tax drag (held)", "Volatility",
                   "Max drawdown"], rows), ""]

    def vol_rows(key: str, bench: dict) -> list:
        def r(label, fn, fb):
            return [label] + [fn(cf[c][key]) for c in CONFIG_ORDER] + [fb(bench)]

        def g(k, d=1):
            return lambda b: f"{f(b[k], d)}%"

        blank = lambda b: ""   # noqa: E731
        return [
            r("Volatility", g("vol", 0), g("vol", 0)),
            r("Max drawdown", g("max_drawdown", 0), g("max_drawdown", 0)),
            r("Worst calendar year", lambda b: f"{f(b['worst_year'])}% ({b['worst_year_label']})",
              lambda b: f"{f(b['worst_year'])}% ({b['worst_year_label']})"),
            r("Worst 12 months", g("worst_12m"), g("worst_12m")),
            r("Longest time under a previous high", lambda b: f"{f(b['longest_underwater_months'], 0)} months",
              lambda b: f"{f(b['longest_underwater_months'], 0)} months"),
            r("Rolling 3-year windows ahead of SPY", g("ahead_3y", 0), blank),
            r("Rolling 5-year windows ahead of SPY", g("ahead_5y", 0), blank),
            r("Longest stretch behind SPY", lambda b: f"{f(b['longest_behind_months'], 0)} months", blank),
            r("Worst 12-month shortfall against SPY", lambda b: f(b['worst_12m_shortfall'], 1, " pts"), blank),
        ]

    out += ["## Volatility and the time behind SPY", "", "Before tax, against SPY before tax:", "",
            table(["", *heads, "SPY"], [["CAGR"] + [f"{f(cf[c]['cagr']['pre'])}%" for c in CONFIG_ORDER]
                                       + [f"{f(sp['cagr_held'])}%"]] + vol_rows("pre", sb["pre"])), "",
            "After tax (each year's tax paid from the account), against SPY after tax:", "",
            table(["", *heads, "SPY"], [["CAGR (held)"] + [f"{f(cf[c]['cagr']['after_held'])}%" for c in CONFIG_ORDER]
                                       + [f"{f(sa['cagr_held'])}%"]] + vol_rows("after", sb["after"])), "",
            "Rolling windows step by month. *Behind SPY* follows the ratio of the two accounts: the longest time it "
            "stayed below its previous high.", ""]

    def trow(label, fn, spy_cell=None):
        cells = [label] + [fn(cf[c]) for c in CONFIG_ORDER]
        return cells + [spy_cell] if spy_cell is not None else cells

    t = lambda x: x["tax"]   # noqa: E731
    out += ["## Taxes", "", table(["", *heads, "SPY"], [
        trow("Tax paid a year (average, % of the account)", lambda x: f"{f(t(x)['tax_per_year_pct'], 2)}%",
             f"{f(res['spy_tax_per_year_pct'], 2)}%"),
        trow("Taxes paid in total", lambda x: f"${t(x)['taxes_total']:,.0f}", f"${sa['taxes_total']:,.0f}"),
        trow("Short-term share of realized gains", lambda x: f(t(x)['st_share_of_gains'], 0, "%"), "–"),
        trow("Wash sales (losses deferred)", lambda x: f"{t(x)['wash_sales']} (${t(x)['wash_disallowed']:,.0f})", "0"),
        trow("Loss carryforward left: short / long term",
             lambda x: f"${t(x)['carry_st']:,.0f} / ${t(x)['carry_lt']:,.0f}", "–"),
        trow("Dividends received", lambda x: f"${t(x)['dividends']:,.0f}", f"${sa['stats'].get('dividends', 0):,.0f}"),
        trow("Ending value before tax", lambda x: f"${x['values']['pre']['marked']:,.0f}",
             f"${sp['values']['marked']:,.0f}"),
        trow("Ending value after tax: held / sold",
             lambda x: f"${x['values']['after']['held']:,.0f} / ${x['values']['after']['sold']:,.0f}",
             f"${sa['values']['held']:,.0f} / ${sa['values']['sold']:,.0f}"),
    ]), "", "From $100,000. A loss carryforward is worth something only to a reader with gains to offset.", ""]

    lc = lambda x: x["leverage_check"]   # noqa: E731
    out += ["## The leverage check: SPY on margin at the same volatility", "",
            f"SPY bought on margin (or partly held in cash) so its volatility matches the strategy's, rebalanced "
            f"monthly. Margin at the 3-month T-bill rate plus {res['margin']['spread']:g}%, at least "
            f"{res['margin']['floor']:g}% ({f(res['margin']['today'])}% today); idle cash earns nothing; same tax "
            "rules.", "",
            table(["", *heads], [
                trow("SPY exposure (x equity)", lambda x: f"{f(lc(x)['leverage'], 2)}x"),
                trow("SPY on margin: before tax", lambda x: f"{f(lc(x)['pre'])}%"),
                trow("SPY on margin: after tax, held / sold",
                     lambda x: f"{f(lc(x)['after_held'])}% / {f(lc(x)['after_sold'])}%"),
                trow("SPY on margin: max drawdown", lambda x: f"{f(lc(x)['max_drawdown'], 0)}%"),
                trow("Margin interest paid (before tax)", lambda x: f"${lc(x)['interest_pct'] * 1000:,.0f}"),
                trow("The strategy: before tax / after tax (held)",
                     lambda x: f"{f(x['cagr']['pre'])}% / {f(x['cagr']['after_held'])}%"),
            ]), ""]

    rows = []
    for c in CONFIG_ORDER:
        pre, post = mc[f"{c}/pre"], mc[f"{c}/after"]
        rows.append([SHORT[c], f"{f(cf[c]['cagr']['pre'])}% / {f(cf[c]['cagr']['after_held'])}%",
                     f"{f(pre['ranked_percentile'], 0)} / {f(post['ranked_percentile'], 0)}",
                     f"{f(pre['p5'])}% / **{f(pre['p50'])}%** / {f(pre['p95'])}%",
                     f"{f(post['p5'])}% / **{f(post['p50'])}%** / {f(post['p95'])}%",
                     f"{f(pre['beat_spy'], 0)}% / {f(post['beat_spy'], 0)}%",
                     f"{f(cf[c]['ret63']['pre'])}% / {f(cf[c]['ret63']['after_held'])}%"])
    out += ["## Selection: the ranking against random selection", "",
            f"{res['seeds']} random selections per row. Before tax / after tax (held).", "",
            table(["", "12-month momentum ranking", "Its percentile among the random runs",
                   "Random: 5th / median / 95th, before tax", "Random: 5th / median / 95th, after tax",
                   "Random runs beating SPY (before / after tax)", "3-month ranking"], rows), ""]

    rows = [["As run"] + [f"{f(cf[c]['cagr']['pre'])}% / {f(cf[c]['cagr']['after_held'])}%" for c in CONFIG_ORDER]]
    for label in ROBUST_LABELS:
        rows.append([label[0].upper() + label[1:]] + [
            f"{f(rb[c][label]['pre'])}% / {f(rb[c][label]['after_held'])}%" if label in rb[c] else "–"
            for c in CONFIG_ORDER])
    rows.append(["Five largest winners made nothing (before tax, approximate)"]
                + [f"{f(cf[c]['without_top5'])}%" for c in CONFIG_ORDER])
    out += ["## Robustness", "",
            "12-month momentum ranking, combined period; CAGR before tax / after tax (held). Defaults: breakout 20 "
            "weeks, rate of change 30%, index average 10 weeks, wide stop 40%, tight stop 10% (WTT only; the "
            "QS-style stop never tightens, so its wide stop moves alone).", "", table(["", *heads], rows), "",
            "The last row subtracts the five largest winners' dollar gains from the ending value, ignoring what that "
            "money compounded into afterwards.", ""]

    rows = []
    for period in ("in_sample", "out_of_sample"):
        for c in CONFIG_ORDER:
            x = cf[c]["periods"][period]
            rows.append([title[period], SHORT[c], f"{f(x['pre']['cagr'])}%", f"{f(x['after_held'])}%",
                         f"{f(x['after_sold'])}%", f"{f(x['pre']['max_drawdown'], 0)}%"])
        x = cf[CONFIG_ORDER[0]]["periods"][period]
        rows.append([title[period], "SPY", f"{f(x['spy_pre'])}%", f"{f(x['spy_after_held'])}%",
                     f"{f(x['spy_after_sold'])}%", ""])
    out += ["## The two periods", "", "Each a separate run from $100,000.", "",
            table(["Period", "", "Before tax", "After tax, held", "After tax, sold", "Max drawdown"], rows), ""]

    tr = lambda x: x["trades"]   # noqa: E731
    out += ["## Trades", "", table(["", *heads], [
        trow("Trades", lambda x: f"{tr(x)['trades']:,}"),
        trow("Win rate", lambda x: f"{f(tr(x)['win_rate'])}%"),
        trow("Average win / loss", lambda x: f"{f(tr(x)['avg_win'], 1, '%', True)} / {f(tr(x)['avg_loss'], 1, '%', True)}"),
        trow("Median holding", lambda x: f"{f(tr(x)['weeks'], 0)} weeks"),
        trow("Average holding: winners / losers", lambda x: f"{f(tr(x)['weeks_win'], 0)} / {f(tr(x)['weeks_loss'], 0)} weeks"),
        trow("Average invested", lambda x: f"{f(x['invested'], 0)}%"),
    ]), "", "Before tax. Partial sales to pay tax count as part of the trade they came from.", ""]

    years_ = list(sb["pre"]["yearly"])
    rows = [[y] + [f"{f(cf[c]['pre']['yearly'].get(y))}% / {f(cf[c]['after']['yearly'].get(y))}%" for c in CONFIG_ORDER]
            + [f"{f(sb['pre']['yearly'][y])}% / {f(sb['after']['yearly'][y])}%"] for y in years_]
    out += ["## Yearly returns", "", "Before tax / after tax (a year's tax comes out at the start of the next year).", "",
            table(["Year", *heads, "SPY"], rows), "", method(res)]
    return "\n".join(out) + "\n"


def method(res: dict) -> str:
    r = res["rates"]
    m = res["margin"]
    return "\n".join([
        "## Method and assumptions", "",
        "Strategies (rules unchanged from the earlier studies; taxes never change a trade):",
        "- *WTT as written*: weekly closes; buy at the next open when the stock closes at a 20-week high, its 20-week "
        "rate of change is 30% or more and SPY is above its 10-week average; a 40% trailing stop on the highest "
        "weekly close that tightens to 10% after any week with SPY below its average and never moves down; 20 "
        "positions of 5%.",
        "- *QS-style*: the same entries; a 40% trailing stop that never tightens; 10 positions of 10%. A "
        "reconstruction of QuantifiedStrategies' members-only rules (see `output/wttqs/report.md`).",
        "- Universe: Russell 3000 or S&P 500 + MidCap 400 members at the signal week (point in time: archived "
        "iShares holdings, the S&P 500 change log, Wikipedia's S&P 400 list checked against IJH), as-traded close "
        "of $1 or more.",
        "- Ranking, when more stocks qualify than slots are free: 12-month momentum skipping the latest month (the "
        "return from 252 to 21 sessions before the signal), highest first, chosen before the run as the "
        "best-documented stock-selection signal. Random selection is the baseline; the 3-month return is an "
        "alternative.",
        "- Costs: slippage 0.10% a side, 0.25% when the as-traded price is under $20.",
        "",
        "The reporting standard:",
        "- Dividends: included, credited at the ex-date to shares held at the previous close (split-adjusted).",
        f"- Taxes: {f(r['short_term'])}% on short-term gains and ordinary dividends, {f(r['long_term'])}% on long-term "
        "gains and qualified dividends (top federal rates with the 3.8% net investment income tax); no state tax. "
        "Lots first in, first out; long-term after more than a year. Short- and long-term results net within the "
        "year; losses carry forward by class; no deduction against other income. Wash sales: a loss on shares bought "
        "back within 30 days is deferred into the new shares (clawed back if the loss was already used). Dividends "
        "are qualified when the shares were held more than 60 days of the 121 around the ex-date.",
        "- Payment: each year's tax comes out of the account at the first session of the next year (so selling to "
        "pay it does not change that year's bill), from cash first and then by selling every position pro rata at "
        "the open. Each period starts from $100,000 with no carryforward.",
        "- Ending value *held*: the tax on everything realized is paid; open positions are not sold. *Sold*: open "
        "positions are sold at the last close and taxed.",
        "- SPY: bought at the first close, dividends reinvested at the ex-date close, the same taxes paid by "
        "selling SPY.",
        f"- Margin (the leverage check only; neither strategy borrows): the 3-month T-bill rate plus "
        f"{m['spread']:g}%, at least {m['floor']:g}%; deductible against short-term gains and ordinary dividends, "
        "the rest carried forward.",
        "- Data limits: delisted stocks are thin before 2021, so 2016–2020 is flattered (more for the Russell 3000). "
        "Russell membership comes from periodic snapshots: a stock counts in a week when the latest snapshot of any "
        "of the three funds, or one from the previous 190 days, holds it.",
        "",
        "Files: `results.json`, `curves_combined.csv` (before- and after-tax curves), `montecarlo.csv`, "
        "`trades_*.csv` (after-tax runs).",
    ])
