"""Episodic pivot backtests and post-signal drift.

Grid: gap 5/10/20% x neglected filter off/on x the three best breakout exits (C10, A5, D10 from
exit_comparison.md), each over the three ground-rule periods. The earnings condition is on.
Drift: every signal (conditions 1-3 on a liquid stock), traded or not, followed for 60 sessions
from the signal day's close.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, _f, run_test  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.episodic_pivot import DEFAULTS, EpisodicPivot, earnings_mask, signal_mask, strong_close  # noqa: E402

GAPS = (5, 10, 20)
EXITS = {
    "C10": ("a third on day 3, breakeven, 10-day SMA trail", {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10}),
    "A5": ("initial stop, time exit after 5 days", {"kind": "time", "days": 5}),
    "D10": ("a third on day 5, breakeven, 10-day SMA trail", {"kind": "partial_day", "day": 5, "fraction": 1 / 3, "n": 10}),
}
HORIZON = 60
SHOW_DAYS = (1, 2, 3, 5, 10, 20, 30, 40, 50, 60)


def variant_name(gap, neglected, code):
    return f"ep_gap{gap}_{'neglected' if neglected else 'all'}_{code}"


def drift(panel, signals: np.ndarray, spy_j: int) -> dict:
    """Cumulative return from the signal close to each of the next HORIZON closes (held flat after a
    delisting), raw and minus SPY over the same days. Signals without a full window are left out."""
    p = panel
    held = _ffill_2d(np.where(p.valid, p.close, np.nan))
    tt, jj = np.nonzero(signals[: len(p.dates) - HORIZON])
    base = p.close[tt, jj]
    steps = np.arange(HORIZON + 1)
    paths = held[tt[:, None] + steps, jj[:, None]] / base[:, None] - 1
    spy = held[tt[:, None] + steps, spy_j] / held[tt, spy_j][:, None] - 1
    return {"t": tt, "j": jj, "raw": paths, "excess": paths - spy}


def _ffill_2d(a: np.ndarray) -> np.ndarray:
    """Carry each column's last valid value forward (a delisted stock stays at its last close)."""
    idx = np.where(np.isfinite(a), np.arange(a.shape[0])[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    return a[idx, np.arange(a.shape[1])]


def summarize(d: dict, rows: np.ndarray) -> dict:
    raw, excess = d["raw"][rows], d["excess"][rows]
    return {"signals": int(rows.sum()),
            "mean_raw": np.nanmean(raw, axis=0), "median_raw": np.nanmedian(raw, axis=0),
            "mean_excess": np.nanmean(excess, axis=0), "median_excess": np.nanmedian(excess, axis=0),
            "beat_spy": np.nanmean(excess > 0, axis=0)}


def main() -> None:
    panel, bench = load_all()
    dates = earnings.load(panel)
    emask = earnings_mask(panel, dates)
    spy_j = panel.index["SPY"]
    lines = ["# Episodic pivot", "", f"Data through {panel.dates[-1]}.", ""]

    # Earnings coverage: how many gap-and-volume days on liquid stocks match a release date.
    study = panel.dates >= "2016-01-01"
    lines += ["## Earnings dates", "",
              f"Per-symbol earnings histories were found for {sum(1 for v in dates.values() if v):,} of "
              f"{len(dates):,} stocks. Share of gap-and-volume signals (liquid stocks, 2016 on) that fall on the "
              "first session after a release:", "", "| Gap | Gap and volume signals | On an earnings day | Share |",
              "|---|---|---|---|"]
    coverage = {}
    for gap in GAPS:
        base = signal_mask(panel, {**DEFAULTS, "gap_pct": gap, "earnings": False}, None) & panel.eligible
        base[~study] = False
        on = base & emask
        coverage[gap] = (int(base.sum()), int(on.sum()))
        lines.append(f"| {gap}% | {base.sum():,} | {on.sum():,} | {100 * on.sum() / base.sum():.0f}% |")
    lines.append("")

    # Strategy grid.
    results = {}
    for gap in GAPS:
        for neglected in (False, True):
            for code, (label, rule) in EXITS.items():
                name = variant_name(gap, neglected, code)
                params = {**DEFAULTS, "gap_pct": gap, "neglected": neglected, "exit": rule}
                desc = (f"Episodic pivot: gap of at least {gap}%, volume at least 3x the 50-day average, first session "
                        f"after an earnings release{', prior 60-session return under 20%' if neglected else ''}. Buy "
                        f"at the close if it is above the open and in the upper half of the range; stop at the day's "
                        f"low. Exit: {label}.")
                results[name] = run_test(panel, name, lambda: EpisodicPivot(dates), params, bench,
                                         description=desc)["results"]
                print(f"{name}: done")
    header = ("| Gap | Neglected | Exit | IS trades | IS expectancy | OOS trades | OOS expectancy | OOS CAGR | "
              "OOS max DD | Combined expectancy | Combined CAGR | Combined max DD |")
    lines += ["## Results (earnings condition on)", "",
              "Each row is a separate test; IS 2016–2021, OOS 2022 onward, combined 2016 onward.", "",
              header, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for gap in GAPS:
        for neglected in (False, True):
            for code in EXITS:
                r = results[variant_name(gap, neglected, code)]
                i, o, c = r["in_sample"], r["out_of_sample"], r["combined"]
                lines.append(
                    f"| {gap}% | {'yes' if neglected else 'no'} | {code} | {i['trades']} | {_f(i['expectancy_r'], 3, 'R')} | "
                    f"{o['trades']} | {_f(o['expectancy_r'], 3, 'R')} | {_f(o['cagr'], 1, '%')} | "
                    f"{_f(o['max_drawdown'], 1, '%')} | {_f(c['expectancy_r'], 3, 'R')} | {_f(c['cagr'], 1, '%')} | "
                    f"{_f(c['max_drawdown'], 1, '%')} |")
    spy = results[variant_name(10, False, "C10")]
    lines += ["", "SPY with dividends: " + "; ".join(
        f"{PERIOD_TITLES[p]} CAGR {spy[p]['spy']['cagr']:+.1f}%, max drawdown {spy[p]['spy']['max_drawdown']:.1f}%"
        for p in PERIODS) + ".", ""]
    best = max(results, key=lambda n: results[n]["in_sample"]["expectancy_r"])
    lines += [f"Best in-sample expectancy: `{best}` ({results[best]['in_sample']['expectancy_r']:+.3f}R in-sample, "
              f"{results[best]['out_of_sample']['expectancy_r']:+.3f}R out-of-sample).", ""]

    # Drift after every signal.
    drift_rows, drift_summary = [], {}
    lines += ["## Returns after every signal (traded or not)", "",
              f"Signals: conditions 1–3 on a liquid stock. Cumulative return from the signal day's close; \"vs SPY\" "
              f"subtracts SPY's return over the same days. Signals without {HORIZON} later sessions are left out.", ""]
    for gap in GAPS:
        for neglected in (False, True):
            sig = signal_mask(panel, {**DEFAULTS, "gap_pct": gap, "neglected": neglected}, emask) & panel.eligible
            sig[~study] = False
            d = drift(panel, sig, spy_j)
            entry = strong_close(panel, DEFAULTS["close_in_range"])[d["t"], d["j"]]
            day = panel.dates[d["t"]]
            groups = {
                "in_sample": day < "2022-01-01", "out_of_sample": day >= "2022-01-01", "combined": np.ones(len(day), bool),
                "combined_entry_condition_met": entry, "combined_entry_condition_not_met": ~entry,
            }
            for gname, rows in groups.items():
                if not rows.any():
                    continue
                s = summarize(d, rows)
                key = (gap, neglected, gname)
                drift_summary[key] = s
                for k in range(1, HORIZON + 1):
                    drift_rows.append([gap, "yes" if neglected else "no", gname, k, s["signals"],
                                       round(100 * s["mean_raw"][k], 3), round(100 * s["median_raw"][k], 3),
                                       round(100 * s["mean_excess"][k], 3), round(100 * s["median_excess"][k], 3),
                                       round(100 * s["beat_spy"][k], 1)])
    for neglected in (False, True):
        lines += [f"### {'Neglected filter on' if neglected else 'All signals'}: mean / median return vs SPY", "",
                  "| Gap | Period | Signals | " + " | ".join(f"Day {k}" for k in SHOW_DAYS) + " |",
                  "|---|---|---|" + "---|" * len(SHOW_DAYS)]
        for gap in GAPS:
            for gname in ("in_sample", "out_of_sample", "combined", "combined_entry_condition_met",
                          "combined_entry_condition_not_met"):
                s = drift_summary.get((gap, neglected, gname))
                if not s:
                    continue
                cells = [f"{100 * s['mean_excess'][k]:+.1f}% / {100 * s['median_excess'][k]:+.1f}%" for k in SHOW_DAYS]
                lines.append(f"| {gap}% | {gname.replace('_', ' ')} | {s['signals']:,} | " + " | ".join(cells) + " |")
        lines.append("")
    with (OUTPUT / "ep_drift_by_day.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["gap_pct", "neglected", "group", "day", "signals", "mean_return_pct", "median_return_pct",
                    "mean_vs_spy_pct", "median_vs_spy_pct", "share_beating_spy_pct"])
        w.writerows(drift_rows)
    path = OUTPUT / "ep_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "ep_comparison.json").write_text(json.dumps(
        {"coverage": coverage, "best_in_sample": best,
         "results": {n: {p: {k: v for k, v in m.items() if k != "equity_curve"} for p, m in r.items()}
                     for n, r in results.items()}}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
