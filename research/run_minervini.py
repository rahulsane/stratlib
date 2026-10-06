"""Minervini trend template + VCP: signal counts per year, then two exits over the ground-rule periods.

Exit (a): half at +20%, the rest on the first close below the 50-day SMA (initial stop until then).
Exit (b): the best breakout exit (C10: a third at the day-3 close, breakeven stop, 10-day SMA trail).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import OUTPUT, PERIOD_TITLES, PERIODS, _f, run_test, simulate, top_share  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.minervini import C10, DEFAULTS, Minervini, new_setups, trend_template, vcp_scan  # noqa: E402

EXITS = {
    "a": ("(a) half at +20%, rest on a close below the 50-day SMA",
          {"kind": "partial_pct", "pct": 20, "fraction": 0.5, "n": 50, "breakeven": False}),
    "b": ("(b) a third on day 3, breakeven, 10-day SMA trail (best breakout exit)", C10),
}
ROWS = [
    ("Trades", lambda m: f"{m['trades']:,}"),
    ("Win rate", lambda m: _f(m["win_rate"], 1, "%")),
    ("Avg win", lambda m: f"{_f(m['avg_win_pct'], 2, '%')} / {_f(m['avg_win_r'], 2, 'R')}"),
    ("Avg loss", lambda m: f"{_f(m['avg_loss_pct'], 2, '%')} / {_f(m['avg_loss_r'], 2, 'R')}"),
    ("Expectancy", lambda m: _f(m["expectancy_r"], 3, "R")),
    ("Profit factor", lambda m: _f(m["profit_factor"], 2)),
    ("CAGR", lambda m: _f(m["cagr"], 1, "%")),
    ("Max drawdown", lambda m: _f(m["max_drawdown"], 1, "%")),
    ("Sharpe", lambda m: _f(m["sharpe"], 2)),
    ("Avg holding", lambda m: f"{_f(m['avg_holding_days'], 1)} days"),
    ("Time with a position / avg invested", lambda m: f"{_f(m['exposure_time_pct'], 0, '%')} / {_f(m['exposure_avg_invested_pct'], 0, '%')}"),
    ("Top 10% of trades, share of profit", top_share),
    ("Breakouts through the pivot", lambda m: f"{m['counts'].get('triggered', 0):,}"),
    ("Sold at the close on low breakout volume", lambda m: f"{m['exit_reasons'].get('breakout volume under 1.4x', 0):,}"),
    ("Skipped: no free slot", lambda m: f"{m['counts'].get('skipped_no_slot', 0):,}"),
]


def main() -> None:
    panel, bench = load_all()
    template, _ = trend_template(panel, DEFAULTS)
    scan = vcp_scan(panel, template, DEFAULTS)
    years = np.array([d[:4] for d in panel.dates])
    year_list = [str(y) for y in range(2016, int(panel.dates[-1][:4]) + 1)]
    fresh = new_setups(scan["setup"], scan["pivot"])
    per_year = {"Trend template stock-days": {y: int(template[years == y].sum()) for y in year_list},
                "VCP setup stock-days": {y: int(scan["setup"][years == y].sum()) for y in year_list},
                "New VCP setups (new stock or pivot)": {y: int(fresh[years == y].sum()) for y in year_list}}
    counts_by_swing = {}
    for swing in (5.0, 8.0):
        s = vcp_scan(panel, template, {**DEFAULTS, "swing_pct": swing})
        f = new_setups(s["setup"], s["pivot"])
        counts_by_swing[swing] = {y: int(f[years == y].sum()) for y in year_list}

    results, trades_by_year = {}, {}
    for code, (label, rule) in EXITS.items():
        name = f"minervini_vcp_exit_{code}"
        summary = run_test(panel, name, lambda: Minervini(scan), {**DEFAULTS, "exit": rule}, bench,
                           description=f"Minervini trend template and VCP, exit {label}. See strategies/minervini.py.")
        results[code] = summary["results"]
        s = Minervini(scan)
        s.setup(panel, {**DEFAULTS, "exit": rule})
        trades = simulate(panel, s, *PERIODS["combined"])["trades"]
        kept = Counter(t.entry_date[:4] for t in trades if "breakout volume" not in t.exit_reason)
        failed = Counter(t.entry_date[:4] for t in trades if "breakout volume" in t.exit_reason)
        trades_by_year[code] = {y: (kept.get(y, 0), failed.get(y, 0)) for y in year_list}
        print(f"exit {code}: done")

    lines = ["# Minervini trend template + VCP", "", f"Data through {panel.dates[-1]}.", "",
             "## Signals per year", "",
             "| | " + " | ".join(year_list) + " |", "|---|" + "---|" * len(year_list)]
    for label, row in per_year.items():
        lines.append(f"| {label} | " + " | ".join(f"{row[y]:,}" for y in year_list) + " |")
    for code in EXITS:
        lines.append(f"| Trades kept / sold on low volume, exit {code} (combined run) | "
                     + " | ".join(f"{k} / {f}" for k, f in (trades_by_year[code][y] for y in year_list)) + " |")
    for swing, row in counts_by_swing.items():
        lines.append(f"| New setups with a {swing:g}% swing threshold (count only) | "
                     + " | ".join(f"{row[y]:,}" for y in year_list) + " |")
    lines += ["", f"VCP funnel, template stock-days (all years): {scan['funnel']}.", ""]
    for period in PERIODS:
        lines += [f"## {PERIOD_TITLES[period]}", "", "| | " + " | ".join(EXITS[c][0] for c in EXITS) + " |",
                  "|---|" + "---|" * len(EXITS)]
        for title, fn in ROWS:
            lines.append(f"| {title} | " + " | ".join(fn(results[c][period]) for c in EXITS) + " |")
        spy = results["a"][period]["spy"]
        lines += ["", f"SPY with dividends: CAGR {spy['cagr']:+.1f}%, max drawdown {spy['max_drawdown']:.1f}%.", ""]
    lines += ["## Yearly returns (combined runs)", "", "| Year | Exit (a) | Exit (b) | SPY with dividends |",
              "|---|---|---|---|"]
    for y in results["a"]["combined"]["yearly"]:
        lines.append(f"| {y} | {results['a']['combined']['yearly'][y]:+.1f}% | {results['b']['combined']['yearly'][y]:+.1f}% | "
                     f"{results['a']['combined']['spy']['yearly'][y]:+.1f}% |")
    path = OUTPUT / "minervini_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "minervini_comparison.json").write_text(json.dumps(
        {"per_year": per_year, "counts_by_swing": counts_by_swing, "funnel": scan["funnel"],
         "trades_by_year": trades_by_year,
         "results": {c: {p: {k: v for k, v in m.items() if k != "equity_curve"} for p, m in r.items()}
                     for c, r in results.items()}}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
