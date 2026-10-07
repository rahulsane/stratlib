"""Qullamaggie breakout (daily bars): 10-day SMA trail as specified, then the 20-day SMA trail.

Both versions were specified in advance, so both run all three periods once.
The worst-case entry-day assumption is run as a bound on the main results.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_rules as rr  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, run_test, top_share  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.qullamaggie import DEFAULTS, Qullamaggie  # noqa: E402

DESCRIPTION = """Daily-bar version of the Qullamaggie breakout, run exactly as specified.

- Setup at the close of T: highest close of the last 60 sessions at least 30% above the lowest low of the 60
  sessions before it; 10–40 sessions since that high; lowest low since the high no more than 25% below it;
  last 5 sessions' range under 12% of the close; close above the 10- and 20-day SMAs; 20-day SMA above its
  value 5 sessions earlier; 20-day average daily range (high − low as % of the low) at least 4%.
- Pivot: highest high from the day of the highest close through T. Buy-stop at the pivot on T+1 to T+5
  (a newer setup on the same stock replaces the order); filled at the pivot, or at the open on a gap.
- Initial stop: low of the session before entry; skipped if more than one 20-day average daily range below
  the entry.
- Exits: a third sold at the close of the third session after entry, stop moved to the entry fill; the rest
  sold at the first later close below the trailing SMA.
- Entry day: an up day is assumed to run open, low, high, close, so a low that preceded an intraday breakout
  does not stop the trade; on a down day, or after a gap above the pivot, any low below the stop does."""

VARIANTS = [
    ("qullamaggie_sma10", {"trail_sma": 10}, Rules(), "10-day SMA trail (as specified)"),
    ("qullamaggie_sma20", {"trail_sma": 20}, Rules(), "20-day SMA trail"),
    ("qullamaggie_sma10_worst_case", {"trail_sma": 10}, Rules(entry_day_path="worst"),
     "10-day SMA trail, worst-case entry day"),
    ("qullamaggie_sma20_worst_case", {"trail_sma": 20}, Rules(entry_day_path="worst"),
     "20-day SMA trail, worst-case entry day"),
]

ROWS = [
    ("Trades", lambda m: f"{m['trades']:,}"),
    ("Win rate", lambda m: _f(m["win_rate"], 1, "%")),
    ("Average win", lambda m: f"{_f(m['avg_win_pct'], 2, '%')} / {_f(m['avg_win_r'], 2, 'R')}"),
    ("Average loss", lambda m: f"{_f(m['avg_loss_pct'], 2, '%')} / {_f(m['avg_loss_r'], 2, 'R')}"),
    ("Expectancy", lambda m: _f(m["expectancy_r"], 3, "R")),
    ("Profit factor", lambda m: _f(m["profit_factor"], 2)),
    ("Total return", lambda m: _f(m["total_return"], 1, "%")),
    ("CAGR", lambda m: _f(m["cagr"], 1, "%")),
    ("Max drawdown", lambda m: _f(m["max_drawdown"], 1, "%")),
    ("Sharpe", lambda m: _f(m["sharpe"], 2)),
    ("Average holding (days)", lambda m: _f(m["avg_holding_days"], 1)),
    ("Time with a position", lambda m: _f(m["exposure_time_pct"], 1, "%")),
    ("Average invested", lambda m: _f(m["exposure_avg_invested_pct"], 1, "%")),
    ("Top 10% of trades, share of profit", top_share),
    ("SPY total return (with dividends)", lambda m: _f(m["spy"]["total_return"], 1, "%")),
    ("SPY CAGR (with dividends)", lambda m: _f(m["spy"]["cagr"], 1, "%")),
]


def rules_section() -> list[str]:
    return rr.section(
        ["This report compares two trailing exits for the Qullamaggie breakout and runs each again with a worst-case "
         "assumption about the entry day. The setup and entry are the same in all four tests."],
        rr.QULLAMAGGIE_SETUP,
        ["Exit: sell a third at the close of the 3rd session after entry and move the stop on the rest up to the entry "
         "price (breakeven). From the next session, sell the rest at the first close below the trailing SMA.", "",
         "- 10-day SMA trail: the exit as Qullamaggie describes it (C10 in the exit-rule report).",
         "- 20-day SMA trail: the same with the 20-day SMA (C20).",
         "- Worst-case entry day: any low below the stop on the entry day is assumed to come after the entry, so the "
         "trade is stopped out that day. This shows how much the up-day assumption above is worth."],
        rr.GROUND_RULES,
        rr.TERMS + ["- Top 10% of trades, share of profit: the net profit of the best tenth of trades as a share of the "
                    "total. When the total is a loss, the dollar results of the best tenth and of the rest are shown "
                    "instead."])


def main() -> None:
    panel, bench = load_all()
    results = {}
    for name, params, rules, label in VARIANTS:
        summary = run_test(panel, name, Qullamaggie, {**DEFAULTS, **params}, bench, rules=rules,
                           description=f"{label}.\n\n{DESCRIPTION}")
        results[name] = (label, summary["results"])
        print(f"{name}: done")
    lines = ["# Qullamaggie breakout: 10-day vs 20-day SMA trail", "",
             f"Data through {panel.dates[-1]}. Reports and trade lists are in each test's folder.", ""]
    lines += rules_section()
    for period in PERIODS:
        lines += [f"## {PERIOD_TITLES[period]}", "",
                  "| | " + " | ".join(label for label, _ in results.values()) + " |",
                  "|---|" + "---|" * len(results)]
        for title, fn in ROWS:
            lines.append(f"| {title} | " + " | ".join(fn(res[period]) for _, res in results.values()) + " |")
        lines.append("")
    lines += ["## Yearly returns (combined runs)", "",
              "| Year | " + " | ".join(label for label, _ in results.values()) + " | SPY with dividends |",
              "|---|" + "---|" * (len(results) + 1)]
    first = next(iter(results.values()))[1]["combined"]
    for year in first["yearly"]:
        cells = [_f(res["combined"]["yearly"][year], 1, "%") for _, res in results.values()]
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {_f(first['spy']['yearly'][year], 1, '%')} |")
    path = OUTPUT / "qullamaggie_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
