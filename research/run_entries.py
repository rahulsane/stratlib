"""Entry-rule comparison for the Qullamaggie breakout setup, with the original exit (a third on day 3,
breakeven stop, 10-day SMA trail), which was also the best exit out-of-sample.

- A-D run the three ground-rule periods. B and D also run without the one-ADR stop limit, because
  their stop (the breakout day's low) is usually wider; both readings were fixed before any run.
- E (opening-range breakouts on one-minute bars) needs intraday data, which covers only the last two
  years, so A-D and E are also compared on that window, each from fresh capital.
- Baseline (A) trades are bucketed by where the breakout day closed in its range.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import OUTPUT, PERIOD_TITLES, PERIODS, _f, run_test, simulate  # noqa: E402
from intraday import Intraday  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.qullamaggie import DEFAULTS, Qullamaggie  # noqa: E402

WINDOW = {"last_2_years": ("2024-09-30", None)}
BASE = {**DEFAULTS, "trail_sma": 10}
FULL = {
    "A": ("A: buy through the pivot (baseline)", {"entry_rule": "pivot"}),
    "B": ("B: strong close", {"entry_rule": "strong_close"}),
    "C": ("C: volume at least 1.5x", {"entry_rule": "volume"}),
    "D": ("D: strong close and volume", {"entry_rule": "strong_close_volume"}),
    "B_nolimit": ("B without the one-ADR stop limit", {"entry_rule": "strong_close", "max_stop_adr": None}),
    "D_nolimit": ("D without the one-ADR stop limit", {"entry_rule": "strong_close_volume", "max_stop_adr": None}),
}
ORB = {
    "E5": ("E: 5-minute opening-range breakout", {"entry_rule": "orb5"}),
    "E30": ("E: 30-minute opening-range breakout", {"entry_rule": "orb30"}),
    "E5_nolimit": ("E5 without the one-ADR stop limit", {"entry_rule": "orb5", "max_stop_adr": None}),
    "E30_nolimit": ("E30 without the one-ADR stop limit", {"entry_rule": "orb30", "max_stop_adr": None}),
}
DESCRIPTION = ("Qullamaggie breakout setup (unchanged) with the original exit: a third sold at the close of the "
               "third session after entry, stop to the entry fill, the rest sold at the first later close below "
               "the 10-day SMA. The one-ADR stop limit applies unless the name says otherwise.")

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
    ("Avg holding (days)", lambda m: _f(m["avg_holding_days"], 1)),
    ("Average invested", lambda m: _f(m["exposure_avg_invested_pct"], 1, "%")),
    ("Breakouts through the pivot", lambda m: f"{m['counts'].get('triggered', 0):,}"),
    ("Not confirmed", lambda m: f"{m['counts'].get('not_confirmed', 0):,}" if "not_confirmed" in m["counts"] else "–"),
    ("Skipped: stop limit", lambda m: f"{m['counts'].get('skipped_stop_rule', 0):,}"),
    ("Sold on low volume", lambda m: f"{m['exit_reasons'].get('breakout volume under 1.5x', 0):,}"),
]


def table(title: str, results: dict, labels: dict) -> list[str]:
    lines = [f"## {title}", "", "| | " + " | ".join(labels[c] for c in results) + " |",
             "|---|" + "---|" * len(results)]
    for name, fn in ROWS:
        lines.append(f"| {name} | " + " | ".join(fn(m) for m in results.values()) + " |")
    spy = next(iter(results.values()))["spy"]
    return lines + ["", f"SPY with dividends: CAGR {spy['cagr']:+.1f}%, max drawdown {spy['max_drawdown']:.1f}%.", ""]


def close_location(panel, trade) -> float:
    j, t = panel.index[trade.ticker], panel.day[trade.entry_date]
    high, low, close = panel.high[t, j], panel.low[t, j], panel.close[t, j]
    return float((close - low) / (high - low)) if high > low else math.nan


def buckets(panel, trades) -> dict:
    groups = {"bottom third": [], "middle third": [], "top third": []}
    for tr in trades:
        loc = close_location(panel, tr)
        if not math.isfinite(loc):
            continue
        groups["bottom third" if loc < 1 / 3 else "middle third" if loc < 2 / 3 else "top third"].append(tr)
    out = {}
    for name, trs in groups.items():
        rs = np.array([t.r for t in trs])
        out[name] = {"trades": len(trs), "expectancy_r": float(rs.mean()) if len(rs) else math.nan,
                     "win_rate": 100 * float((rs > 0).mean()) if len(rs) else math.nan,
                     "entry_day_stops": sum(1 for t in trs if t.exit_date == t.entry_date),
                     "total_r": float(rs.sum())}
    return out


def main() -> None:
    panel, bench = load_all()
    intraday = Intraday(panel)
    labels = {c: v[0] for c, v in {**FULL, **ORB}.items()}
    full, window = {}, {}
    for code, (label, extra) in FULL.items():
        summary = run_test(panel, f"entries_{code}", Qullamaggie, {**BASE, **extra}, bench,
                           description=f"{label}. {DESCRIPTION}")
        full[code] = summary["results"]
        print(f"{code}: done")
    for code, (label, extra) in {**FULL, **ORB}.items():
        summary = run_test(panel, f"entries_{code}_last2y", lambda: Qullamaggie(intraday), {**BASE, **extra}, bench,
                           periods=WINDOW, description=f"{label}, last two years. {DESCRIPTION}")
        window[code] = summary["results"]["last_2_years"]
        print(f"{code} (last two years): done")

    lines = ["# Entry rules for the Qullamaggie breakout", "", f"Data through {panel.dates[-1]}. {DESCRIPTION}", ""]
    main_codes = ["A", "B", "C", "D"]
    for period in PERIODS:
        lines += table(PERIOD_TITLES[period], {c: full[c][period] for c in main_codes}, labels)
    for period in PERIODS:
        lines += table(f"{PERIOD_TITLES[period]}: B and D without the one-ADR stop limit",
                       {c: full[c][period] for c in ("B_nolimit", "D_nolimit")}, labels)
    start = WINDOW["last_2_years"][0]
    lines += table(f"Last two years ({start} to {panel.dates[-1]}), with one-minute data for E",
                   {c: window[c] for c in ("A", "B", "C", "D", "E5", "E30")}, labels)
    lines += table("Last two years: without the one-ADR stop limit",
                   {c: window[c] for c in ("B_nolimit", "D_nolimit", "E5_nolimit", "E30_nolimit")}, labels)
    lines += [f"One-minute data: {intraday.missing} candidate days had no bars and {intraday.mismatched} disagreed "
              "with the daily bar by more than 1.5%; E cannot enter on those days.", ""]

    bucket_results = {}
    lines += ["## Baseline trades by where the breakout day closed in its range", "",
              "Close location = (close - low) / (high - low) of the entry day. Entry-day stops are trades stopped "
              "out on the breakout day itself.", "",
              "| Period | Bucket | Trades | Win rate | Expectancy | Total R | Entry-day stops |", "|---|---|---|---|---|---|---|"]
    for period in ("in_sample", "out_of_sample", "combined"):
        start, end = PERIODS[period]
        s = Qullamaggie()
        s.setup(panel, {**BASE, **FULL["A"][1]})
        trades = simulate(panel, s, start, end)["trades"]
        bucket_results[period] = buckets(panel, trades)
        for name, b in bucket_results[period].items():
            lines.append(f"| {PERIOD_TITLES[period]} | {name} | {b['trades']} | {_f(b['win_rate'], 1, '%')} | "
                         f"{b['expectancy_r']:+.3f}R | {b['total_r']:+.1f}R | {b['entry_day_stops']} |")
    path = OUTPUT / "entry_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "entry_comparison.json").write_text(json.dumps(
        {"full": {c: {p: {k: v for k, v in m.items() if k != "equity_curve"} for p, m in res.items()}
                  for c, res in full.items()},
         "last_2_years": {c: {k: v for k, v in m.items() if k != "equity_curve"} for c, m in window.items()},
         "buckets": bucket_results, "intraday": {"missing": intraday.missing, "mismatched": intraday.mismatched}},
        indent=1, default=float), encoding="utf-8")
    intraday.close()
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
