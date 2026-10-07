"""Exit-rule comparison on the exact entries of the Qullamaggie breakout test (10-day SMA version, as specified).

For each exit rule and period:
- Per-trade figures use every recorded entry, replayed without the 10-position and cash limits, so
  all rules are measured on the same trades.
- Portfolio figures (CAGR, drawdown, Sharpe, exposure, yearly returns) come from a replay under the
  ground rules (10 positions, no margin); entries that find no slot or cash there are counted.
All rules were specified before any was run, so each runs the out-of-sample period once.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_rules as rr  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, run_test, simulate  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.exit_replay import ExitReplay, entries_from_test  # noqa: E402
from strategies.qullamaggie import DEFAULTS, Qullamaggie  # noqa: E402

EXITS = {
    "A5": ("A: initial stop, time exit after 5 days", {"kind": "time", "days": 5}),
    "A10": ("A: initial stop, time exit after 10 days", {"kind": "time", "days": 10}),
    "A20": ("A: initial stop, time exit after 20 days", {"kind": "time", "days": 20}),
    "A40": ("A: initial stop, time exit after 40 days", {"kind": "time", "days": 40}),
    "B10": ("B: first close below the 10-day SMA", {"kind": "sma", "n": 10}),
    "B20": ("B: first close below the 20-day SMA", {"kind": "sma", "n": 20}),
    "B50": ("B: first close below the 50-day SMA", {"kind": "sma", "n": 50}),
    "C10": ("C: a third on day 3, breakeven, 10-day SMA trail (original)",
            {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10}),
    "C20": ("C: a third on day 3, breakeven, 20-day SMA trail",
            {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 20}),
    "D10": ("D: a third on day 5, breakeven, 10-day SMA trail",
            {"kind": "partial_day", "day": 5, "fraction": 1 / 3, "n": 10}),
    "D20": ("D: a third on day 5, breakeven, 20-day SMA trail",
            {"kind": "partial_day", "day": 5, "fraction": 1 / 3, "n": 20}),
    "E": ("E: half at +2R, breakeven, 20-day SMA trail", {"kind": "partial_r", "r": 2, "fraction": 0.5, "n": 20}),
    "F": ("F: chandelier, highest high minus 3 x 14-day ATR", {"kind": "chandelier", "mult": 3, "atr": 14}),
}
UNLIMITED = Rules(max_positions=10**6, allow_margin=True)
LONG = 20  # "held longer than 20 days": more than 20 trading sessions


def rules_section() -> list[str]:
    return rr.section(
        ["This report takes the trades of the Qullamaggie breakout test (10-day SMA version) and replays them with "
         "13 exit rules. The signals, entry days, entry prices and initial stops are the same for every exit; only "
         "the way out changes."],
        rr.QULLAMAGGIE_SETUP,
        ["Exit rules. The initial stop stays in place under every rule, as a floor.", "", *rr.exits(*EXITS)],
        ["Two ways of measuring:", "",
         "- Per-trade columns (expectancy to *held > 20 days*) replay every recorded entry with no position or cash "
         "limit, so all 13 exits are measured on the same trades. A stock cannot be held twice, so an entry that "
         "arrives while its stock is still held under a slow exit is replayed separately.",
         "- Portfolio columns (CAGR, drawdown, Sharpe, average invested) replay the entries under the portfolio rules "
         "below. A slow exit ties up slots and cash, so some entries find no room; *blocked* counts them.",
         f"- *Held > {LONG} days* is the share of the total R that came from trades held more than {LONG} sessions. "
         "When the total is a loss, the R of the longer trades and of the rest are shown instead.",
         "- Max DD (rank) ranks the exits from the smallest drawdown (1) to the largest."],
        rr.GROUND_RULES, rr.TERMS)


def trade_stats(trades) -> dict:
    rs = np.array([t.r for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    long_r = float(sum(t.r for t in trades if t.holding_sessions > LONG))
    total_r = float(rs.sum())
    return {
        "trades": len(trades), "win_rate": 100 * len(wins) / len(rs), "expectancy_r": float(rs.mean()),
        "avg_win_r": float(wins.mean()) if len(wins) else math.nan,
        "avg_loss_r": float(losses.mean()) if len(losses) else math.nan,
        "avg_win_pct": float(np.mean([t.return_pct for t in trades if t.r > 0])),
        "avg_loss_pct": float(np.mean([t.return_pct for t in trades if t.r <= 0])),
        "profit_factor_r": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else math.inf,
        "total_r": total_r, "long_trades": sum(1 for t in trades if t.holding_sessions > LONG),
        "long_r": long_r, "short_r": total_r - long_r,
        "long_share": 100 * long_r / total_r if total_r > 0 else math.nan,
        "avg_sessions": float(np.mean([t.holding_sessions for t in trades])),
        "avg_days": float(np.mean([t.holding_days for t in trades])),
    }


def all_entry_trades(panel, period_entries, rule, period, start, end) -> list:
    """Every recorded entry as its own trade. Without position or cash limits trades do not affect each
    other, except that a stock cannot be held twice; an entry that arrives while its stock is still held
    under this exit rule is replayed in a further pass."""
    trades, remaining = [], list(period_entries)
    while remaining:
        s = ExitReplay({period: remaining})
        s.setup(panel, {"exit": rule})
        s.begin_period(period)
        run = simulate(panel, s, start, end, UNLIMITED)
        done = {(panel.index[t.ticker], panel.day[t.entry_date]) for t in run["trades"]}
        assert done, "no progress"
        trades += run["trades"]
        remaining = [e for e in remaining if (e.j, e.entry_day) not in done]
    assert len(trades) == len(period_entries)
    return trades


def long_share(s: dict) -> str:
    if math.isfinite(s["long_share"]):
        return f"{s['long_share']:.0f}% ({s['long_trades']} trades)"
    return f"net loss: {s['long_trades']} longer trades {s['long_r']:+.1f}R, the rest {s['short_r']:+.1f}R"


def main() -> None:
    panel, bench = load_all()
    params = {**DEFAULTS, "trail_sma": 10}
    entries = entries_from_test(panel, Qullamaggie, params, PERIODS)
    per_trade, portfolio = {}, {}
    for code, (label, rule) in EXITS.items():
        factory = lambda: ExitReplay(entries)  # noqa: E731
        summary = run_test(panel, f"exits_{code}", factory,
                           {"exit": rule, "entries": "qullamaggie_sma10 (as specified)"}, bench,
                           description=f"{label}. Entries replayed from the Qullamaggie breakout test "
                                       f"(10-day SMA version): same signals, entry prices and initial stops.")
        portfolio[code] = summary["results"]
        per_trade[code] = {}
        for period, (start, end) in PERIODS.items():
            per_trade[code][period] = trade_stats(all_entry_trades(panel, entries[period], rule, period, start, end))
        print(f"{code}: done")

    lines = ["# Exit rules on the Qullamaggie breakout entries", "",
             f"Data through {panel.dates[-1]}. Entries: the Qullamaggie breakout test as specified "
             f"({', '.join(f'{PERIOD_TITLES[p]} {len(entries[p])}' for p in PERIODS)} entries).", "",
             "- Per-trade columns use every recorded entry, replayed without position or cash limits, so all "
             "exits are measured on the same trades.",
             "- Portfolio columns (CAGR, drawdown, Sharpe, exposure) replay the entries under the portfolio rules below "
             "(10 positions, no margin); \"blocked\" counts entries that found no slot or cash there, or whose "
             "stock was still held from an earlier entry.",
             f"- \"Held > {LONG} days\" is the share of the total R from trades held more than {LONG} trading "
             "sessions.", ""]
    lines += rules_section()
    for period in ("out_of_sample", "in_sample", "combined"):
        order = sorted(EXITS, key=lambda c: -per_trade[c][period]["expectancy_r"])
        dd_rank = {c: k + 1 for k, c in enumerate(sorted(EXITS, key=lambda c: portfolio[c][period]["max_drawdown"]))}
        lines += [f"## {PERIOD_TITLES[period]}, ranked by expectancy", "",
                  "| Rank | Exit | Expectancy | Win rate | Avg win | Avg loss | PF (R) | Avg hold | "
                  f"Held > {LONG} days, share of R | CAGR | Max DD (rank) | Sharpe | Avg invested | Blocked |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, c in enumerate(order, 1):
            s, m = per_trade[c][period], portfolio[c][period]
            blocked = sum(m["counts"].get(k, 0) for k in ("skipped_no_slot", "skipped_no_cash", "skipped_already_held"))
            lines.append(
                f"| {k} | {c}: {EXITS[c][0].split(': ', 1)[1]} | {s['expectancy_r']:+.3f}R | {s['win_rate']:.1f}% | "
                f"{s['avg_win_pct']:+.2f}% / {s['avg_win_r']:+.2f}R | {s['avg_loss_pct']:+.2f}% / {s['avg_loss_r']:+.2f}R | "
                f"{_f(s['profit_factor_r'], 2)} | {s['avg_sessions']:.1f} sessions | {long_share(s)} | "
                f"{m['cagr']:+.1f}% | {m['max_drawdown']:.1f}% ({dd_rank[c]}) | {m['sharpe']:.2f} | "
                f"{m['exposure_avg_invested_pct']:.1f}% | {blocked} |")
        spy = portfolio["A5"][period]["spy"]
        lines += ["", f"SPY with dividends: CAGR {spy['cagr']:+.1f}%, max drawdown {spy['max_drawdown']:.1f}%, "
                      f"Sharpe {spy['sharpe']:.2f}.", ""]
    lines += ["## Yearly returns (combined portfolio runs)", "",
              "| Year | " + " | ".join(EXITS) + " | SPY |", "|---|" + "---|" * (len(EXITS) + 1)]
    for year in portfolio["A5"]["combined"]["yearly"]:
        cells = [f"{portfolio[c]['combined']['yearly'][year]:+.1f}%" for c in EXITS]
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {portfolio['A5']['combined']['spy']['yearly'][year]:+.1f}% |")
    path = OUTPUT / "exit_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "exit_comparison.json").write_text(json.dumps(
        {"per_trade": per_trade, "portfolio": {c: {p: {k: v for k, v in r.items() if k != "equity_curve"}
                                                   for p, r in res.items()} for c, res in portfolio.items()},
         "exits": {c: {"label": l, "rule": r} for c, (l, r) in EXITS.items()}}, indent=1, default=float),
        encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
