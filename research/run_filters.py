"""Market filters on the best configuration of each strategy.

- Breakout: Qullamaggie setup, buy through the pivot, C10 exit (the original test).
- Episodic pivot: 20% gap, no neglected filter, C10 exit.
- Minervini: trend template + VCP, exit (a) (half at +20%, rest on the 50-day SMA).
Filters (market_filters.py) apply on the signal day: no new signals while the filter is off.
Unfiltered trades are then split by each filter's state on their signal day.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
import market_filters as mf  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, _f, run_test, simulate  # noqa: E402
from lab import load_all  # noqa: E402
from strategies import episodic_pivot as ep  # noqa: E402
from strategies import minervini as mv  # noqa: E402
from strategies import qullamaggie as qm  # noqa: E402

MINERVINI_EXIT_A = {"kind": "partial_pct", "pct": 20, "fraction": 0.5, "n": 50, "breakeven": False}


def strategies(panel):
    dates = earnings.load(panel)
    template, _ = mv.trend_template(panel, mv.DEFAULTS)
    scan = mv.vcp_scan(panel, template, mv.DEFAULTS)
    return {
        "breakout": ("Breakout (Qullamaggie, pivot entry, C10 exit)", qm.Qullamaggie(),
                     {**qm.DEFAULTS, "trail_sma": 10}),
        "episodic_pivot": ("Episodic pivot (20% gap, C10 exit)", ep.EpisodicPivot(dates),
                           {**ep.DEFAULTS, "gap_pct": 20, "neglected": False, "exit": ep.C10}),
        "minervini": ("Minervini VCP (exit a)", mv.Minervini(scan), {**mv.DEFAULTS, "exit": MINERVINI_EXIT_A}),
    }


def split(trades, panel, mask) -> dict:
    out = {}
    for state, flag in (("on", True), ("off", False)):
        rs = np.array([t.r for t in trades if bool(mask[panel.day[t.signal_date]]) == flag])
        out[state] = {"trades": len(rs), "expectancy_r": float(rs.mean()) if len(rs) else math.nan,
                      "win_rate": 100 * float((rs > 0).mean()) if len(rs) else math.nan,
                      "sd": float(rs.std(ddof=1)) if len(rs) > 1 else math.nan, "total_r": float(rs.sum())}
    on, off = out["on"], out["off"]
    if on["trades"] > 1 and off["trades"] > 1:
        se = math.sqrt(on["sd"] ** 2 / on["trades"] + off["sd"] ** 2 / off["trades"])
        out["difference"] = on["expectancy_r"] - off["expectancy_r"]
        out["t"] = out["difference"] / se if se > 0 else math.nan
    return out


def main() -> None:
    panel, bench = load_all()
    filters = mf.compute(panel)
    masks = filters["masks"]
    results, splits, shared = {}, {}, {}
    for key, (label, inner, params) in strategies(panel).items():
        results[key] = {}
        for code, mask in masks.items():
            name = f"filter_{key}_{code}"
            summary = run_test(panel, name, lambda: mf.Filtered(inner, mask, code, shared),
                               {**params, "market_filter": code}, bench,
                               description=f"{label}, market filter {code}: {mf.LABELS[code]} (on the signal day).")
            results[key][code] = summary["results"]
            print(f"{name}: done")
        splits[key] = {}
        for period, (start, end) in PERIODS.items():
            wrapper = mf.Filtered(inner, masks["A"], "A", shared)
            wrapper.setup(panel, params)
            trades = simulate(panel, wrapper, start, end)["trades"]
            splits[key][period] = {code: split(trades, panel, masks[code]) for code in "BCDEF"}

    lines = ["# Market filters", "", f"Data through {panel.dates[-1]}. Filters are checked at the signal day's close; "
             "while a filter is off no new signals are taken (open positions are unaffected).", "",
             "| Filter | Definition | Share of OOS days on |", "|---|---|---|"]
    oos_days = panel.dates >= "2022-01-01"
    for code, label in mf.LABELS.items():
        lines.append(f"| {code} | {label} | {100 * masks[code][oos_days].mean():.0f}% |")
    lines.append("")
    for key, (label, _, _) in strategies_labels().items():
        lines += [f"## {label}", "", "| Filter | OOS trades | OOS expectancy | OOS CAGR | OOS max DD | "
                  "IS expectancy | IS CAGR | Combined expectancy | Combined CAGR | Combined max DD |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for code in masks:
            o, i, c = (results[key][code][p] for p in ("out_of_sample", "in_sample", "combined"))
            lines.append(f"| {code}: {mf.LABELS[code]} | {o['trades']} | {_f(o['expectancy_r'], 3, 'R')} | "
                         f"{_f(o['cagr'], 1, '%')} | {_f(o['max_drawdown'], 1, '%')} | {_f(i['expectancy_r'], 3, 'R')} | "
                         f"{_f(i['cagr'], 1, '%')} | {_f(c['expectancy_r'], 3, 'R')} | {_f(c['cagr'], 1, '%')} | "
                         f"{_f(c['max_drawdown'], 1, '%')} |")
        spy = results[key]["A"]["out_of_sample"]["spy"]
        lines += ["", f"SPY out-of-sample: CAGR {spy['cagr']:+.1f}%, max drawdown {spy['max_drawdown']:.1f}%.", "",
                  "Unfiltered trades split by the filter's state on their signal day:", "",
                  "| Filter | Period | On: trades / expectancy | Off: trades / expectancy | On minus off (t) |",
                  "|---|---|---|---|---|"]
        for code in "BCDEF":
            for period in ("in_sample", "out_of_sample"):
                s = splits[key][period][code]
                diff = f"{s['difference']:+.3f}R ({s['t']:+.1f})" if "difference" in s else "–"
                lines.append(f"| {code} | {PERIOD_TITLES[period]} | {s['on']['trades']} / {_f(s['on']['expectancy_r'], 3, 'R')} | "
                             f"{s['off']['trades']} / {_f(s['off']['expectancy_r'], 3, 'R')} | {diff} |")
        lines.append("")
    path = OUTPUT / "filter_comparison.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "filter_comparison.json").write_text(json.dumps(
        {"results": {k: {c: {p: {kk: vv for kk, vv in m.items() if kk != "equity_curve"} for p, m in r.items()}
                         for c, r in res.items()} for k, res in results.items()},
         "splits": splits, "days_on_oos": {c: float(masks[c][oos_days].mean()) for c in masks}},
        indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


def strategies_labels():
    return {"breakout": ("Breakout (Qullamaggie, pivot entry, C10 exit)", None, None),
            "episodic_pivot": ("Episodic pivot (20% gap, C10 exit)", None, None),
            "minervini": ("Minervini VCP (exit a)", None, None)}


if __name__ == "__main__":
    main()
