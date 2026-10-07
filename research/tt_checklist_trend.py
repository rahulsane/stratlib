"""Compare D10 with three fixed, entry-only 200-session trend gates.

Baseline: D10, portfolio D ranked quarterly by trailing 63-session return, top ten.
M: previous-session SPY close strictly above its trailing 200-session SMA.
S: previous-session stock close strictly above its trailing 200-session SMA.
MS: both conditions. A full window of valid closes is required. Gates apply
after the original top-ten selection; rejected names are not backfilled.

Existing positions retain D10's quarterly screen/rank exits and original 20%
fixed stops. Gates do not force sales. Cash is split equally among permitted
new orders under the original 33 1/3% entry cap; retained positions are not
resized. Entry gates are tested only on the original quarterly rebalance dates.
Stops and blocked entries wait in cash until a subsequent quarterly entry.

The original same-close ranking/execution convention, slippage, price-only
stock returns, no margin, and zero interest on idle cash remain unchanged.
Only the new trend signals are explicitly lagged. The 2022+ split is inherited
and is exploratory, not fresh out-of-sample validation. No parameter sweep.
"""
# Run: .venv/Scripts/python.exe research/tt_checklist_trend.py

from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

import benchmarks
import panel as P
import report_rules as rr
from engine import OUTPUT, PERIODS, PERIOD_TITLES, Rules, run_test, simulate
from strategies.tt_checklist_top10 import RankedChecklist
from strategies.tt_checklist_trend import TrendGatedChecklist
from tt_checklist_compare import ROWS, load
from tt_checklist_top10 import cells, checklist_parts, holdings


BASE = "tt_checklist_qual_top10"
OUT = OUTPUT / "traveling_trader"
REPORT = OUT / "checklist_top10_trend.md"
GATES = {"D10 + M": "new entries only while SPY's previous close is above its 200-session SMA",
         "D10 + S": "new entries only while the stock's previous close is above its 200-session SMA",
         "D10 + MS": "new entries only while both SPY and the stock closed above their 200-session SMAs"}
VARIANTS = {
    "D10": (BASE, False, False),
    "D10 + M": (BASE + "_market200", True, False),
    "D10 + S": (BASE + "_stock200", False, True),
    "D10 + MS": (BASE + "_both200", True, True),
}
DISPLAY_PERIODS = ["combined", "out_of_sample", "in_sample"]


def run_description(label: str) -> str:
    """The run's description in the report's variation panel: its gate, then the study's rules."""
    return f"{label}: {GATES[label]}.\n\n{__doc__}"


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def verify_trade_records(actual, expected):
    assert len(actual) == len(expected)
    for new, old in zip(actual, expected):
        for key, value in asdict(new).items():
            if key not in old:
                continue
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                # The engine's trade export rounds numeric fields to four decimals.
                np.testing.assert_allclose(round(value, 4), float(old[key]), rtol=0, atol=1e-10)
            elif value is None:
                assert old[key] == ""
            elif isinstance(value, bool):
                assert old[key] == str(value)
            else:
                assert str(value) == str(old[key]), (key, value, old[key])


def gate_summary(strategy, trades, start, end):
    """Count selected and unheld names at each actual entry decision.

    Existing trades with exit_date == decision date have already exited before
    the new-entry step. A same-date entry is consequently a new opportunity.
    """
    rows = [r for r in strategy.gate_audit if start <= r["rebalance_date"] < end]
    decisions = []
    for row in rows:
        day, symbol = row["rebalance_date"], row["ticker"]
        retained = any(t["ticker"] == symbol and t["entry_date"] < day < t["exit_date"] for t in trades)
        decisions.append({**row, "already_held_after_exits": retained,
                          "new_purchase_blocked": not retained and not row["entry_permitted"]})
    quarter_rows = {r["rebalance_date"]: r for r in rows}
    return {
        "quarters": len(quarter_rows),
        "quarters_spy_below_or_missing": sum(not r["spy_above_sma200"] for r in quarter_rows.values()),
        "top10_selections": len(rows),
        "selections_below_stock_sma_or_missing": sum(not r["stock_above_sma200"] for r in rows),
        "unheld_top10_candidates_blocked": sum(r["new_purchase_blocked"] for r in decisions),
        "stock_sma_missing": sum(not np.isfinite(r["prior_stock_sma200"]) for r in rows),
    }, decisions


def rules_section() -> list[str]:
    return rr.section(
        ["This report adds trend filters to D10, the top-10 version of the Traveling Trader checklist portfolio. A "
         "filter can only block a new purchase; it never sells."],
        *checklist_parts(),
        ["The filters, checked on each quarterly entry date with the previous session's prices:", "",
         "- M: SPY's close above its 200-session simple moving average.",
         "- S: the stock's own close above its 200-session simple moving average.",
         "- MS: both.",
         "", "The top ten are chosen first; a name the filter blocks is not replaced by the 11th, and its share of "
         "the cash goes to the other new names. Held stocks are not sold when a filter turns off."],
        rr.TERMS)


def main():
    report_only = "--report-only" in sys.argv
    saved_path = OUTPUT / BASE / "results.json"
    saved = json.loads(saved_path.read_text(encoding="utf-8"))
    screen_path = OUT / "checklist_fwd_screen.json"
    screen = json.loads(screen_path.read_text(encoding="utf-8"))
    panel = P.load(through=saved["data_through"])
    assert str(panel.dates[-1]) == screen["data_through"] == saved["data_through"]
    through = str(panel.dates[-1])
    rules = Rules(**saved["rules"])
    passes = {int(k): frozenset(v) for k, v in screen["passes_qual"].items()}
    prior_audit = json.loads((OUT / "checklist_top10.json").read_text(encoding="utf-8"))
    screen_hash = hashlib.sha256(screen_path.read_bytes()).hexdigest()
    assert screen_hash == prior_audit["screen_sha256"]
    bench = benchmarks.load()
    data = {"D10": load(BASE)}
    verification = {}
    if report_only:
        previous = json.loads((OUT / "checklist_top10_trend.json").read_text(encoding="utf-8"))
        assert previous["screen_sha256"] == screen_hash
        assert previous["baseline_results_sha256"] == hashlib.sha256(saved_path.read_bytes()).hexdigest()
        verification = previous["baseline_verified"]
    RankedChecklist.passes = passes
    TrendGatedChecklist.passes = passes
    off_params = {**saved["params"], "trend_sessions": 200, "market_gate": False, "stock_gate": False}

    if not report_only:
        print("Reproducing D10, including equity, holdings, and every trade field...", flush=True)
    for period, (start, end) in ({} if report_only else PERIODS).items():
        strategy = RankedChecklist()
        strategy.setup(panel, saved["params"])
        run = simulate(panel, strategy, start, end, rules)
        np.testing.assert_allclose(run["equity"], saved["results"][period]["equity_curve"]["equity"],
                                   rtol=1e-12, atol=1e-7)
        verify_trade_records(run["trades"], data["D10"]["trades"][period])
        np.testing.assert_array_equal(run["held"], holdings(data["D10"]["trades"][period], run["dates"]))
        disabled = TrendGatedChecklist()
        disabled.setup(panel, off_params)
        off = simulate(panel, disabled, start, end, rules)
        np.testing.assert_array_equal(off["equity"], run["equity"])
        assert [asdict(t) for t in off["trades"]] == [asdict(t) for t in run["trades"]]
        verification[period] = {"equity_reproduced": True, "all_trade_fields_reproduced": True,
                                "holdings_reproduced": True, "disabled_gates_identical": True,
                                "trades": len(run["trades"])}
        print(f"  {period}: exact baseline and disabled-gate match, {len(run['trades'])} trades", flush=True)
    if not report_only:
        del strategy, disabled, run, off

    stats, audit_stats = {}, {}
    quarterly = []
    signal_maps = {}
    for label, (name, market_gate, stock_gate) in VARIANTS.items():
        params = {**off_params, "market_gate": market_gate, "stock_gate": stock_gate}
        if label != "D10":
            if not report_only:
                print(f"Running {label}: frozen 200-session previous-close entry gates...", flush=True)
                run_test(panel, name, TrendGatedChecklist, params, bench, rules=rules,
                         description=run_description(label))
            cached = json.loads((OUTPUT / name / "results.json").read_text(encoding="utf-8"))
            assert cached["params"] == params and cached["rules"] == saved["rules"]
            assert cached["data_through"] == through
            data[label] = load(name)
        strategy = TrendGatedChecklist()
        strategy.setup(panel, params)
        signal_maps[label] = {(r["rebalance_date"], r["ticker"]): r for r in strategy.gate_audit}
        if label != "D10":
            write_csv(OUTPUT / name / "quarterly_gate_signals.csv", strategy.gate_audit)
        for period in PERIODS:
            m, c, t = cells(data[label], period)
            stats[f"{label} {period}"] = {
                **{k: v for k, v in c.items() if k not in ("dates", "equity")}, **t,
                "end_equity": m["end_equity"], "sharpe": m["sharpe"],
                "exposure": m["exposure_avg_invested_pct"],
                "top10pct_profit_share": m["top10pct_profit_share"],
            }
            assert t["max_open"] <= 10
            assert m["counts"]["skipped_no_slot"] == 0
            for trade in data[label]["trades"][period]:
                i, j = panel.session_index(trade["entry_date"]), panel.index[trade["ticker"]]
                assert j in strategy.passes[i] and j in strategy.entry_passes[i]
            gate_stats, decisions = gate_summary(strategy, data[label]["trades"][period],
                                                 m["equity_curve"]["dates"][0],
                                                 m["equity_curve"]["dates"][-1])
            audit_stats[f"{label} {period}"] = gate_stats
            if label != "D10":
                write_csv(OUTPUT / name / f"entry_decisions_{period}.csv", decisions)
            if period == "combined":
                for day in sorted({r["rebalance_date"] for r in decisions}):
                    rows = [r for r in decisions if r["rebalance_date"] == day]
                    quarterly.append({"variant": label, "date": day,
                                      "spy_above_sma200": rows[0]["spy_above_sma200"],
                                      "top10_count": len(rows),
                                      "entry_permitted_count": sum(r["entry_permitted"] for r in rows),
                                      "unheld_candidates_blocked": sum(r["new_purchase_blocked"] for r in rows)})
            print(f"  {period}: {m['trades']} trades, CAGR {c['cagr']:.3f}%, "
                  f"DD {abs(c['max_dd']):.3f}%, Sharpe {m['sharpe']:.3f}", flush=True)
        del strategy

    lines = ["# The top-ten checklist portfolio (D10) with market and stock trend filters", "", __doc__.strip(), "",
             f"Data through {through}. Each period starts from $100,000.", "",
             *rules_section(),
             "## What changed", "",
             "D10 is the original ranked top-ten portfolio. M adds the SPY trend gate, S adds the stock trend gate, "
             "and MS requires both. The top ten and their rank-based exits are determined before either gate. "
             "The gate uses the prior session's price and SMA, including that prior session in the average.", "",
             "Only new purchases are blocked. A retained stock can remain below its own average or during a weak market. "
             "There is no immediate market exit, no rank-11 replacement, and no entry between quarterly rebalances. "
             "If some new names are rejected, the cash-split rule can give the remaining new names larger allocations. "
             "The maximum entry weight stays 33 1/3%; this is not a constant 10%-per-stock portfolio.", "",
             "The unfiltered baseline reproduced every saved equity observation and trade field in all three periods. "
             "The new strategy with both gates disabled also matched it exactly. The price data and the quarterly screens were held fixed.", ""]
    for period in DISPLAY_PERIODS:
        table = {label: cells(data[label], period) for label in VARIANTS}
        lines += [f"## {PERIOD_TITLES[period]}", "", "| Metric | " + " | ".join(VARIANTS) + " |",
                  "|---|" + "---|" * len(VARIANTS)]
        for title, formatter in ROWS:
            if title == "Max drawdown (SPY)":
                formatter = lambda m, c, t: f"{abs(c['max_dd']):.1f}% ({m['spy']['max_drawdown']:.1f}%)"
            lines.append(f"| {title} | " + " | ".join(formatter(*table[label]) for label in VARIANTS) + " |")
        lines += ["| Ending value of $100,000 | " + " | ".join(
            f"${table[label][0]['end_equity']:,.0f}" for label in VARIANTS) + " |", ""]

    lines += ["## Yearly returns, combined runs", "", "| Year | " + " | ".join(VARIANTS) + " | SPY with dividends |",
              "|---|" + "---|" * (len(VARIANTS) + 1)]
    base = data["D10"]["results"]["combined"]
    for year in base["yearly"]:
        year_label = f"{year} through {through[5:]}" if year == through[:4] else year
        lines.append(f"| {year_label} | " + " | ".join(
            f"{data[label]['results']['combined']['yearly'][year]:+.1f}%" for label in VARIANTS)
            + f" | {base['spy']['yearly'][year]:+.1f}% |")

    lines += ["", "## Filter activity, combined runs", "",
              "Counts refer to quarterly decisions, not daily market conditions. Blocked candidates are top-ten names "
              "not already held after that day's ordinary exits. They are not hypothetical completed trades.", "",
              "| Measure | " + " | ".join(VARIANTS) + " |", "|---|" + "---|" * len(VARIANTS)]
    for title, key in [("Quarterly decisions", "quarters"),
                       ("Quarters with SPY below/equal SMA or missing", "quarters_spy_below_or_missing"),
                       ("Top-ten selections across quarters", "top10_selections"),
                       ("Selections below/equal own SMA or missing", "selections_below_stock_sma_or_missing"),
                       ("Selections with incomplete 200-session history", "stock_sma_missing"),
                       ("Unheld top-ten candidates blocked", "unheld_top10_candidates_blocked")]:
        lines.append(f"| {title} | " + " | ".join(str(audit_stats[f"{label} combined"][key]) for label in VARIANTS) + " |")

    missed = []
    for trade in sorted(data["D10"]["trades"]["combined"], key=lambda t: -t["r"]):
        key = (trade["entry_date"], trade["ticker"])
        rejected = {label: not signal_maps[label][key]["entry_permitted"] for label in list(VARIANTS)[1:]}
        if any(rejected.values()):
            missed.append({"ticker": trade["ticker"], "entry": trade["entry_date"], "exit": trade["exit_date"],
                           "baseline_return_pct": float(trade["return_pct"]), "rejected": rejected})
    lines += ["", "## Original D10 entries blocked: six largest subsequent winners", "",
              "Returns belong to the original D10 trades. A filtered variant might buy the stock at a later rebalance; "
              "these are examples of altered entry timing, not an additive estimate of lost portfolio return.", "",
              "| Ticker | Original entry | Original exit | D10 trade return | M blocks | S blocks | MS blocks |",
              "|---|---|---|---|---|---|---|"]
    for row in missed[:6]:
        lines.append(f"| {row['ticker']} | {row['entry']} | {row['exit']} | {row['baseline_return_pct']:+.1f}% | "
                     + " | ".join("Yes" if row["rejected"][label] else "No" for label in list(VARIANTS)[1:]) + " |")

    lines += ["", "## Ten largest winners by R, combined runs", ""]
    for label in VARIANTS:
        trades = data[label]["trades"]["combined"]
        total_r = sum(t["r"] for t in trades)
        lines += [f"### {label}, total {total_r:+.1f}R", "",
                  "| Ticker | Entry | Exit | Return | R | Share of total R | Exit reason |",
                  "|---|---|---|---|---|---|---|"]
        for t in sorted(trades, key=lambda tr: -tr["r"])[:10]:
            lines.append(f"| {t['ticker']} | {t['entry_date']} | {t['exit_date']} | {float(t['return_pct']):+.1f}% | "
                         f"{t['r']:+.2f} | {100 * t['r'] / total_r:.1f}% | {t['exit_reason']} |")
        lines.append("")

    lines += ["## Exit reasons", ""]
    for period in DISPLAY_PERIODS:
        lines += [f"### {PERIOD_TITLES[period]}", "", "| Reason | " + " | ".join(VARIANTS) + " |",
                  "|---|" + "---|" * len(VARIANTS)]
        reasons = sorted({r for label in VARIANTS for r in data[label]["results"][period]["exit_reasons"]})
        for reason in reasons:
            lines.append(f"| {reason} | " + " | ".join(
                str(data[label]["results"][period]["exit_reasons"].get(reason, 0)) for label in VARIANTS) + " |")
        lines.append("")

    lines += ["## Interpretation", "",
              "None of the three entry filters improved D10 over the combined period or the 2022+ period: "
              "each had lower CAGR, lower Sharpe, and a larger maximum drawdown. These results do not support "
              "adding these particular entry gates to D10.", ""]
    for label in list(VARIANTS)[1:]:
        c, o = stats[f"{label} combined"], stats[f"{label} out_of_sample"]
        bc, bo = stats["D10 combined"], stats["D10 out_of_sample"]
        lines += [f"{label}: combined CAGR {c['cagr']:.1f}% versus {bc['cagr']:.1f}% for D10, "
                  f"maximum drawdown {abs(c['max_dd']):.1f}% versus {abs(bc['max_dd']):.1f}%, "
                  f"and Sharpe {c['sharpe']:.2f} versus {bc['sharpe']:.2f}. "
                  f"Average investment was {c['exposure']:.0f}% versus {bc['exposure']:.0f}%. "
                  f"In 2022 onward, CAGR was {o['cagr']:.1f}% versus {bo['cagr']:.1f}%, "
                  f"and drawdown was {abs(o['max_dd']):.1f}% versus {abs(bo['max_dd']):.1f}%. "
                  f"Later-window expectancy excluding the five best trades was {o['exp_wo_top5']:+.2f}R.", ""]
    lines += ["The market gate admitted new purchases at the January and April 2022 rebalances, then blocked "
              "July and October 2022 and January 2023. It also blocked April 2020. Original D10 entries in "
              "CDNS, LSCC and ACLS were among those rejected. The gated version lost 8.3% in 2020 versus D10's "
              "16.9% gain, and lost 2.8% in 2023 versus D10's 14.0% gain. Its reduced market exposure did not "
              "compensate for the changed entry timing.", "",
              "The stock filter left average exposure almost unchanged, 91% versus 92%, but reduced the average "
              "number of holdings from 9.0 to 7.2. The largest new allocation rose from about 10% to 33%. "
              "The same cash-split sizing rule concentrates capital when fewer purchases qualify. In 2022, "
              "the stock-filter run lost 38.4% versus D10's 27.3%.", "",
              "The stock filter looked better on early-period drawdown, 24.5% versus 30.1%, with similar CAGR, "
              "but that pattern did not persist in 2022 onward. Its later-window drawdown rose to 37.9%, "
              "and its CAGR fell to 3.1% from D10's 5.6%.", "",
              "These runs test purchase restrictions, not market-timing liquidation. A trend gate cannot prevent losses "
              "on retained holdings. Changes in returns reflect both the stocks skipped and subsequent cash allocation "
              "among the remaining purchases. Lower exposure and greater single-name concentration can occur together.", "",
              "## Limits and definitions", "",
              "- All four variants use the same frozen price panel, quarterly trailing-fundamental screen, dates, "
              "cash treatment, and transaction assumptions. Only the entry gates differ. No trend length was optimized.",
              "- The 2022+ label preserves the previous report's split; it is not fresh validation after earlier results "
              "were inspected. These are exploratory hypothetical simulations.",
              "- The new trend rules use only prices through the prior session. D10 still ranks at the same close used "
              "for execution, as in the earlier study. This inherited convention and unrevised point-in-time "
              "fundamental availability remain limitations. Delisted coverage is thin before 2021.",
              "- Moving averages use split-adjusted price closes without dividends or forward filling. Missing/nonpositive "
              "closes make a 200-session window unavailable, and the relevant gate fails.",
              "- Stocks exclude dividends; SPY includes them. Idle cash earns zero. Slippage is 0.10% per side, "
              "or 0.25% below an as-traded $20. Taxes and commissions are absent.",
              "- A 20% stop may lose more on a gap. It stays at the original entry level for retained holdings. "
              "The 33 1/3% cap applies when buying; winning positions can subsequently grow beyond it.",
              "- Turnover is annual purchases divided by average equity. Information ratio uses CAGR minus SPY CAGR "
              "divided by annualized tracking error. Underwater duration uses trading sessions divided by 21.",
              "- Initial weights use the earlier report's post-close equity denominator and may slightly exceed "
              "the nominal cap after trading costs. Post-close position counts exclude positions sold that day.",
              "- Trade expectancy standard errors are descriptive; overlapping trades are not independent. "
              "Removing the five best trades changes the trade statistic, not the simulated equity curve. "
              "Top-trade profit shares can exceed 100% when other trades lose money.",
              "- Each period starts from fresh capital. The combined run's calendar-2022 return can differ from "
              "the standalone 2022+ run because of holdings carried from 2021.", "",
              "## Files", "",
              "- The study JSON download holds the comparison and the validation record.",
              "- Each run's results, trades and equity curves are under *Compare variations*.", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    payload = {"description": __doc__, "data_through": through, "baseline_verified": verification,
               "screen_sha256": screen_hash, "baseline_results_sha256": hashlib.sha256(saved_path.read_bytes()).hexdigest(),
               "rules": asdict(rules), "base_params": saved["params"], "trend_sessions": 200,
               "variants": VARIANTS, "metrics": stats, "gate_activity": audit_stats,
               "blocked_original_entries": missed,
               "validation": {"every_trade_in_original_top10_and_permitted": True,
                              "all_portfolios_at_most_10_positions": True, "no_orders_rejected_for_slots": True}}
    (OUT / "checklist_top10_trend.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_csv(OUT / "checklist_top10_trend_quarters.csv", quarterly)
    print(f"Saved {REPORT}", flush=True)


if __name__ == "__main__":
    main()
