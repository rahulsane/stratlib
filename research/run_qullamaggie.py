"""Qullamaggie breakout (daily bars): 10-day SMA trail as specified, then the 20-day SMA trail.

Both versions were specified in advance, so both run all three periods once.
The worst-case entry-day assumption is run as a bound on the main results.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
