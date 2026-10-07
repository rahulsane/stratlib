"""Portfolio D with a quarterly top-10 momentum selection, fixed before this run.

The ranking is the 63-session price return the other backtests rank by, descending, with
ticker ties. Only passing, liquid stocks with known momentum are eligible. At
each quarterly close, sell failures and names outside the top 10; retain the
shares and initial stops of continuing names. Split available cash among the
selected new entries, subject to D's 33 1/3% entry cap. This is equal allocation
of entry cash, not a quarterly reset of every holding to 10% of equity.

The original 20% fixed stop, quarterly entry dates, slippage, no margin,
non-interest-bearing idle cash, and price-only stock returns are unchanged.
SPY includes dividends. Fewer than 10 passers means fewer holdings. A stop exit
waits in cash until the next quarterly entry date. The 2022+ window has already
been inspected in the parent study, so this extension is exploratory.
"""
# Run: .venv\Scripts\python.exe research\tt_checklist_top10.py

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmarks
import panel as P
import report_rules as rr
from engine import OUTPUT, PERIODS, PERIOD_TITLES, Rules, run_test, simulate
from strategies.tt_checklist import Checklist
from strategies.tt_checklist_top10 import RankedChecklist
from tt_checklist_compare import ROWS, curve_stats, load, trade_stats


NAME = "tt_checklist_qual_top10"
BASE = "tt_checklist_qual_ew"
OUT = OUTPUT / "traveling_trader"
PARAMS = {"stop_pct": 20.0, "equal_weight": True, "top_n": 10,
          "ranking": "return_63_sessions_desc_ticker_asc", "rerank_existing": True}
RULES = Rules(risk_pct=1.0, max_positions=10, max_position_pct=100 / 3)
SOURCE = "https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-47"


def holdings(trades, dates):
    """Post-close holdings. Sells precede new entries in this strategy's engine."""
    counts = np.zeros(len(dates), dtype=int)
    for trade in trades:
        a, b = np.searchsorted(dates, [trade["entry_date"], trade["exit_date"]])
        counts[a:b] += 1
    return counts


def cells(data, period):
    m = data["results"][period]
    c = curve_stats(m["equity_curve"], 100_000.0)
    t = trade_stats(data["trades"][period], c["dates"], c["equity"], c["years"])
    counts = holdings(data["trades"][period], np.array(m["equity_curve"]["dates"]))
    t["avg_open"], t["max_open"] = float(counts.mean()), int(counts.max())
    # Include the first partial month, which the old comparison omitted.
    dates = np.array(m["equity_curve"]["dates"])
    month_end = [i for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]] + [len(dates) - 1]
    eq = np.r_[100_000, np.array(m["equity_curve"]["equity"])[month_end]]
    spy = np.r_[100_000, np.array(m["equity_curve"]["spy_total"])[month_end]]
    mr, sr = eq[1:] / eq[:-1] - 1, spy[1:] / spy[:-1] - 1
    c.update(worst_month=float(mr.min() * 100), worst_month_spy=float(sr.min() * 100),
             months_beat_spy=float((mr > sr).mean() * 100))
    return m, c, t


def checklist_parts() -> list[list[str]]:
    """The checklist, variant D and D10, for this report and the trend-filter follow-up."""
    return [
        rr.CHECKLIST_SOURCE,
        rr.CHECKLIST_TRAILING,
        rr.CHECKLIST_UNIVERSE,
        ["Portfolio D, the starting point (the *trailing-only checklist* in the portfolio comparison): "
         + rr.CHECKLIST_PORTFOLIO[0][len("The portfolio: "):] + " The cash on hand is split equally among that "
         "session's buy orders, each capped at a third of equity, with at most 20 positions; when more stocks pass "
         "than slots are free, the highest 63-session returns are bought first."],
        ["D10: at each quarterly close, rank the passing liquid stocks by their 63-session price return, highest "
         "first, and select the top 10. Sell holdings that failed the checklist or fell out of the top 10; keep the "
         "rest with their shares and original stops. Split the cash on hand equally among the newly selected names, "
         "each capped at a third of equity. A stock stopped out between rebalances leaves its cash idle until the "
         "next one. *What changed* below gives the differences from D in detail."],
        ["Otherwise, as in the other stock backtests here: separate runs from $100,000 for 2016–2021, 2022 on and 2016 on; "
         "0.10% slippage a side, 0.25% under $20 as traded; no margin; idle cash earns nothing; stock returns without "
         "dividends; SPY with dividends as the benchmark. Statements are as restated, and delisted stocks are thin "
         "before 2021. *Limits and definitions* at the end defines the risk measures."],
    ]


def rules_section() -> list[str]:
    return rr.section(
        ["This report changes how portfolio D of the Traveling Trader checklist picks its stocks. Instead of buying "
         "every stock that passes, it ranks the passers by momentum each quarter and holds the top 10."],
        *checklist_parts(), rr.TERMS)


def main():
    original = json.loads((OUTPUT / BASE / "results.json").read_text(encoding="utf-8"))
    panel = P.load(through=original["data_through"])
    bench = benchmarks.load()
    screen_path = OUT / "checklist_fwd_screen.json"
    screen = json.loads(screen_path.read_text(encoding="utf-8"))
    assert screen["data_through"] == original["data_through"] == str(panel.dates[-1])
    passes = {int(k): frozenset(v) for k, v in screen["passes_qual"].items()}
    for day, count in screen["pass_counts_qual"]:
        row = panel.session_index(day)
        assert len(passes[row]) == count

    print("Reproducing the original D from its saved rules and cached trailing screen...", flush=True)
    baseline = load(BASE)
    verification = {}
    Checklist.passes = passes
    for period, (start, end) in PERIODS.items():
        strategy = Checklist()
        strategy.setup(panel, original["params"])
        run = simulate(panel, strategy, start, end, Rules(**original["rules"]))
        saved = original["results"][period]
        np.testing.assert_allclose(run["equity"], saved["equity_curve"]["equity"], rtol=1e-12, atol=1e-7)
        assert len(run["trades"]) == saved["trades"]
        np.testing.assert_array_equal(holdings(baseline["trades"][period], run["dates"]), run["held"])
        verification[period] = {"equity_reproduced": True, "trades": len(run["trades"]),
                                "max_positions": int(run["held"].max()),
                                "average_positions": float(run["held"].mean())}
        print(f"  {period}: reproduced {len(run['trades'])} trades and every equity observation", flush=True)

    print("Running the frozen quarterly top-10 rule...", flush=True)
    RankedChecklist.passes = passes
    summary = run_test(panel, NAME, RankedChecklist, PARAMS, bench, rules=RULES, description=__doc__)
    ranked = RankedChecklist()
    ranked.setup(panel, PARAMS)
    with (OUTPUT / NAME / "quarterly_rankings.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(ranked.rank_audit[0]))
        writer.writeheader()
        writer.writerows(ranked.rank_audit)
    data = {BASE: baseline, NAME: load(NAME)}
    stats = {}
    for test in data:
        for period in PERIODS:
            m, c, t = cells(data[test], period)
            stats[f"{test} {period}"] = {
                **{k: v for k, v in c.items() if k not in ("dates", "equity")}, **t,
                "end_equity": m["end_equity"], "sharpe": m["sharpe"],
                "exposure": m["exposure_avg_invested_pct"], "top10pct_profit_share": m["top10pct_profit_share"],
            }
            if test == NAME:
                assert t["max_open"] <= 10
                assert m["counts"]["skipped_no_slot"] == 0
                for trade in data[test]["trades"][period]:
                    i = panel.session_index(trade["entry_date"])
                    j = int(np.flatnonzero(panel.symbols == trade["ticker"])[0])
                    assert j in ranked.passes[i]

    lines = ["# Portfolio D: ranking the screen and taking the top 10", "", __doc__.strip(), "",
             f"Data through {panel.dates[-1]}. Each period starts from $100,000.", "",
             *rules_section(),
             "## What changed", "",
             "The original D offered every passer to a 20-position book, ranked new orders by 63-session return, "
             "and held existing positions until a stop or screen failure. D10 first selects the top 10 from all "
             "passers and also sells existing holdings that fall below the cutoff. It splits entry cash only "
             "among selected new names. This changes selection, rank-based exits, concentration, and cash deployment.", "",
             "The original sizing hook divided cash by all new candidates, including orders that would later "
             "be rejected for lack of slots. Its label 'fully invested' was therefore inaccurate. The new "
             "selected set fits the available slots. Continuing positions are not resized, so idle cash can "
             "still remain after stops or when the entry cap binds.", "",
             "The prior comparison counted both outgoing and incoming stocks on a rebalance day and reported "
             "35 simultaneous positions for D. Actual post-close holdings never exceeded 20. Both columns "
             "below use corrected daily counts, verified against the simulator. Return figures for original D "
             "are unchanged; its complete equity curves were reproduced before this test.", ""]
    for period in ["combined", "out_of_sample", "in_sample"]:
        table = {test: cells(data[test], period) for test in data}
        lines += [f"## {PERIOD_TITLES[period]}", "", "| Metric | Original D | D10: top 10 by 63-session return |", "|---|---|---|"]
        for title, formatter in ROWS:
            if title == "Max drawdown (SPY)":
                formatter = lambda m, c, t: f"{abs(c['max_dd']):.1f}% ({m['spy']['max_drawdown']:.1f}%)"
            lines.append(f"| {title} | " + " | ".join(formatter(*table[test]) for test in data) + " |")
        lines.append("| Ending value of $100,000 | " + " | ".join(f"${table[test][0]['end_equity']:,.0f}" for test in data) + " |")
        lines.append("")

    lines += ["## Yearly returns, combined runs", "", "| Year | Original D | D10 | SPY with dividends | D10 minus SPY |", "|---|---|---|---|---|"]
    combined = summary["results"]["combined"]
    for year, value in combined["yearly"].items():
        base = original["results"]["combined"]["yearly"][year]
        spy = combined["spy"]["yearly"][year]
        label = f"{year} through Sept 29" if year == "2026" else year
        lines.append(f"| {label} | {base:+.1f}% | {value:+.1f}% | {spy:+.1f}% | {value - spy:+.1f} pts |")

    trades = data[NAME]["trades"]["combined"]
    total_r = sum(t["r"] for t in trades)
    lines += ["", f"## Ten largest winners by R, combined run, total {total_r:+.1f}R", "",
              "| Ticker | Entry | Exit | Return | R | Share of total R | Exit reason |", "|---|---|---|---|---|---|---|"]
    for t in sorted(trades, key=lambda tr: -tr["r"])[:10]:
        lines.append(f"| {t['ticker']} | {t['entry_date']} | {t['exit_date']} | {float(t['return_pct']):+.1f}% | "
                     f"{t['r']:+.2f} | {100 * t['r'] / total_r:.1f}% | {t['exit_reason']} |")
    lines += ["", "## Exit reasons", "", "| Reason | 2016-2021 | 2022-2026 | Combined |", "|---|---|---|---|"]
    reasons = sorted({r for m in summary["results"].values() for r in m["exit_reasons"]})
    for reason in reasons:
        lines.append(f"| {reason} | " + " | ".join(str(summary["results"][p]["exit_reasons"].get(reason, 0)) for p in PERIODS) + " |")

    sc = stats[f"{NAME} combined"]
    so = stats[f"{NAME} out_of_sample"]
    lines += ["", "## Interpretation", "",
              f"D10 returned {sc['cagr']:.1f}% annually against original D's "
              f"{stats[f'{BASE} combined']['cagr']:.1f}% and SPY's {sc['cagr_spy']:.1f}%. "
              f"In the later window it returned {so['cagr']:.1f}% against SPY's {so['cagr_spy']:.1f}%.", "",
              f"The extra return came with greater risk. Combined volatility increased from "
              f"{stats[f'{BASE} combined']['vol']:.1f}% to {sc['vol']:.1f}%, and maximum drawdown from "
              f"{abs(stats[f'{BASE} combined']['max_dd']):.1f}% to {abs(sc['max_dd']):.1f}%. "
              f"Sharpe stayed at {sc['sharpe']:.2f}; Calmar fell to {sc['calmar']:.2f}.", "",
              f"Average investment rose from {stats[f'{BASE} combined']['exposure']:.0f}% to {sc['exposure']:.0f}%. "
              "The difference cannot be attributed solely to better-ranked stocks: rank exits and cash deployment changed too.", "",
              f"There were fewer trades, but more money traded: annual purchase turnover rose from "
              f"{stats[f'{BASE} combined']['turnover']:.0f}% to {sc['turnover']:.0f}%. "
              f"The rank cutoff caused {combined['exit_reasons'].get('fell outside top 10 at a rebalance', 0)} "
              "additional exit decisions. Bigger entries and shorter holding periods account for the greater turnover.", "",
              f"The later-window trade expectancy was {so['expectancy']:+.2f}R with a naive standard error "
              f"of {so['se']:.2f}R; removing the five best trades leaves {so['exp_wo_top5']:+.2f}R. "
              "Trades overlap in time, so this standard error is descriptive and is not an independent-trade significance test.", "",
              "## Limits and definitions", "",
              "- The 2022+ label preserves the earlier report's period split. It is not fresh validation: this follow-up "
              "was proposed after seeing that period's earlier results. The ranking was fixed before this run; no alternatives were searched.",
              "- This uses the same trailing screen and filing-date gates as portfolio D, with no forward-earnings estimates. "
              "It does not prove every vendor fundamental is an unrevised point-in-time value. Delisted coverage is thin before 2021.",
              "- Stocks exclude dividends; SPY includes them. Slippage is 0.10% per side, or 0.25% below an as-traded $20. "
              "Taxes and commissions are absent. Close-based ranks trade at that same close, as in the earlier checklist backtests.",
              "- A 20% stop may lose more on a gap. It stays at the original entry level for retained holdings.",
              "- Turnover is annual purchases divided by average equity. The information ratio follows the prior report's "
              "CAGR-minus-SPY divided by annualized tracking error. Underwater time is trading sessions divided by 21.",
              "- The top-trade profit share can exceed 100% when the remaining trades lose money. "
              "Removing the top five changes the trade statistic; it is not a portfolio rerun without those trades.",
              "- Each period begins from fresh capital. The 2022 return in the combined run can differ from "
              "the standalone 2022+ run because the combined run carries positions from 2021.",
              f"- Results are hypothetical historical simulations, consistent with the [SEC's distinction between backtested "
              f"and actual performance]({SOURCE}).", "",
              "## Files", "",
              "- The study JSON download holds the comparison metrics and the validation record.",
              "- Each run's results, trades and equity curves are under *Compare variations*.", ""]
    report_path = OUT / "checklist_top10.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    payload = {"rules": __doc__, "data_through": str(panel.dates[-1]), "params": PARAMS,
               "screen_sha256": hashlib.sha256(screen_path.read_bytes()).hexdigest(),
               "baseline_verified": verification, "metrics": stats,
               "quarterly_selection_counts": {str(panel.dates[t]): len(js) for t, js in ranked.passes.items()}}
    (OUT / "checklist_top10.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved {report_path}", flush=True)
    for period in PERIODS:
        m = summary["results"][period]
        print(f"{period}: {m['trades']} trades, CAGR {m['cagr']:.3f}%, "
              f"drawdown {m['max_drawdown']:.3f}%, Sharpe {m['sharpe']:.3f}", flush=True)


if __name__ == "__main__":
    main()
