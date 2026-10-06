"""Episodic pivot (20% gap, C10 exit, no market filter) at higher risk per trade.

Risk 0.5% (the ground rule), 1%, 2%, 3% and 5% of equity, each with the 20% position cap kept and
with it lifted (positions limited only by the cash available; still no margin).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, run_test  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.episodic_pivot import C10, DEFAULTS, EpisodicPivot  # noqa: E402

RISKS = (0.5, 1.0, 2.0, 3.0, 5.0)
CAPS = {"cap20": 20.0, "nocap": 100.0}
PARAMS = {**DEFAULTS, "gap_pct": 20, "neglected": False, "exit": C10}


def trade_risk_stats(folder: Path, period: str, summary: dict) -> dict:
    """Worst single trade and average position, as a share of equity before entry."""
    curve = summary["results"][period]["equity_curve"]
    equity = dict(zip(curve["dates"], curve["equity"]))
    dates = curve["dates"]
    losses, sizes = [], []
    for row in csv.DictReader(open(folder / f"trades_{period}.csv", encoding="utf-8")):
        i = max(dates.index(row["entry_date"]) - 1, 0) if row["entry_date"] in equity else 0
        before = equity[dates[i]] if i > 0 else summary["rules"]["capital"]
        losses.append(float(row["pnl"]) / before)
        sizes.append(float(row["position_value"]) / before)
    return {"worst_trade_pct": 100 * min(losses), "avg_position_pct": 100 * float(np.mean(sizes)),
            "max_position_pct": 100 * max(sizes)}


def main() -> None:
    panel, bench = load_all()
    dates = earnings.load(panel)
    results = {}
    for cap_code, cap in CAPS.items():
        for risk in RISKS:
            name = f"ep_gap20_all_C10_risk{risk:g}_{cap_code}"
            rules = Rules(risk_pct=risk, max_position_pct=cap)
            summary = run_test(panel, name, lambda: EpisodicPivot(dates), PARAMS, bench, rules=rules,
                               description=f"Episodic pivot, 20% gap, C10 exit, {risk:g}% risk per trade, "
                                           f"{'20% position cap' if cap < 100 else 'no position cap (cash only)'}.")
            results[(cap_code, risk)] = {p: {**summary["results"][p],
                                             **trade_risk_stats(OUTPUT / name, p, summary)} for p in PERIODS}
            print(f"{name}: done")
    lines = ["# Episodic pivot at higher risk per trade", "",
             "20% gap, C10 exit, no market filter. Same signals; only the sizing changes.", ""]
    for period in ("out_of_sample", "combined", "in_sample"):
        lines += [f"## {PERIOD_TITLES[period]}", "",
                  "| Position cap | Risk per trade | Trades | Expectancy | CAGR | Max drawdown | Sharpe | Worst year | "
                  "Avg position | Avg invested | Worst trade (% of equity) | Cash-limited entries |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for cap_code in CAPS:
            for risk in RISKS:
                m = results[(cap_code, risk)][period]
                worst_year = min(m["yearly"].items(), key=lambda kv: kv[1])
                lines.append(
                    f"| {'20%' if cap_code == 'cap20' else 'none'} | {risk:g}% | {m['trades']} | {_f(m['expectancy_r'], 3, 'R')} | "
                    f"{_f(m['cagr'], 1, '%')} | {_f(m['max_drawdown'], 1, '%')} | {_f(m['sharpe'], 2)} | "
                    f"{worst_year[0]} {worst_year[1]:+.1f}% | {m['avg_position_pct']:.1f}% | "
                    f"{_f(m['exposure_avg_invested_pct'], 1, '%')} | {m['worst_trade_pct']:.1f}% | "
                    f"{m['counts'].get('cash_limited_entries', 0)} |")
        spy = results[("cap20", 0.5)][period]["spy"]
        lines += ["", f"SPY with dividends: CAGR {spy['cagr']:+.1f}%, max drawdown {spy['max_drawdown']:.1f}%, "
                      f"Sharpe {spy['sharpe']:.2f}.", ""]
    lines += ["## Yearly returns, combined runs, no position cap", "",
              "| Year | " + " | ".join(f"{r:g}% risk" for r in RISKS) + " | SPY |", "|---|" + "---|" * (len(RISKS) + 1)]
    base = results[("nocap", 0.5)]["combined"]
    for y in base["yearly"]:
        lines.append(f"| {y} | " + " | ".join(f"{results[('nocap', r)]['combined']['yearly'][y]:+.1f}%" for r in RISKS)
                     + f" | {base['spy']['yearly'][y]:+.1f}% |")
    path = OUTPUT / "ep_risk_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "ep_risk_comparison.json").write_text(json.dumps(
        {f"{c}_{r:g}": {p: {k: v for k, v in m.items() if k != "equity_curve"} for p, m in res.items()}
         for (c, r), res in results.items()}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
