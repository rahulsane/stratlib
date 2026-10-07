"""Episodic pivot (20% gap, SPY above its 50-day SMA) with the liquidity and breakeven corrections.

Corrections: liquidity measured before the signal day (20-day average dollar volume over the prior 20
sessions >= $20M, prior close >= $5, no stock whose split adjustment varies more than 100-fold over
2016 on); the day-3 sale happens only above the entry price.
Exits: (a) C10 with the breakeven fix, (b) initial stop and a 20-session time exit, (c) initial stop
until session 20, then the first close below the 10-day SMA. Each runs standalone at 0.5% risk and as
an SPY overlay at 0.5%, 1.0% and 1.5% risk.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
import market_filters as mf  # noqa: E402
import report_rules as rr  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, run_test, simulate  # noqa: E402
from lab import load_all  # noqa: E402
from strategies import episodic_pivot as ep  # noqa: E402

EXITS = {
    "a": ("(a) C10, with the day-3 sale only above the entry price",
          {"kind": "partial_day", "day": 3, "fraction": 1 / 3, "n": 10, "require_profit": True}),
    "b": ("(b) initial stop, time exit after 20 sessions", {"kind": "time", "days": 20}),
    "c": ("(c) initial stop to day 20, then a close below the 10-day SMA", {"kind": "time_then_sma", "days": 20, "n": 10}),
}
MODES = {
    "alone_0.5": ("standalone, 0.5% risk", Rules()),
    "overlay_0.5": ("SPY overlay, 0.5% risk", Rules(overlay_spy=True, risk_pct=0.5)),
    "overlay_1.0": ("SPY overlay, 1.0% risk", Rules(overlay_spy=True, risk_pct=1.0)),
    "overlay_1.5": ("SPY overlay, 1.5% risk", Rules(overlay_spy=True, risk_pct=1.5)),
}
BASE = {**ep.DEFAULTS, "gap_pct": 20, "neglected": False, "liquidity": "prior", "max_split_multiple": 100,
        "split_window_start": "2016-01-01", "market_filter": "B"}
TWO = ("in_sample", "out_of_sample")


def rules_section() -> list[str]:
    return rr.section(
        ["This report reruns the SPY-filtered episodic pivot (20% gap, no neglected condition, market filter B: SPY "
         "closed above its 50-day SMA on the signal day) with two corrections, three exits and four ways of holding "
         "the capital. The earlier episodic-pivot reports used the uncorrected rules."],
        rr.EPISODIC_PIVOT,
        ["The two corrections:", "",
         "- Liquidity. " + rr.EP_PRIOR_LIQUIDITY[0] + " The earlier reports tested the signal day itself, so a stock "
         "that only became liquid on the news could pass.",
         "- Breakeven. In C10 the third sold on day 3 is sold, and the stop moved to the entry price, only when the "
         "day-3 close is above the entry price. Otherwise nothing is sold, the original stop stays, and the whole "
         "position trails the 10-day SMA from the next session. The earlier version sold the third and moved the "
         "stop even at a loss."],
        ["Exits:", "",
         "- (a) C10 with the breakeven fix, as above.",
         "- (b) Keep only the initial stop and sell at the close of the 20th session after entry.",
         "- (c) Keep only the initial stop until the 20th session after entry; from that close on, sell at the first "
         "close below the 10-day SMA."],
        ["Each exit runs four ways:", "",
         "- Standalone at 0.5% risk per trade, idle cash earning nothing.",
         "- As an SPY overlay at 0.5%, 1.0% and 1.5% risk per trade. " + rr.SPY_OVERLAY[0],
         "",
         "Section 1 lists the trades of the earlier filtered run that the liquidity fix removes. Expectancy is shown "
         "± its standard error. The yearly table subtracts SPY's return with dividends from the overlay's."],
        rr.GROUND_RULES, rr.TERMS)


def expectancy(folder: Path, period: str) -> tuple[float, float, int]:
    rs = np.array([float(r["r"]) for r in csv.DictReader(open(folder / f"trades_{period}.csv", encoding="utf-8"))])
    return float(rs.mean()), float(rs.std(ddof=1) / math.sqrt(len(rs))), len(rs)


def main() -> None:
    panel, bench = load_all()
    dates = earnings.load(panel)
    masks = mf.compute(panel)["masks"]
    lines = ["# Episodic pivot, corrected", "", f"Data through {panel.dates[-1]}. {__doc__.split(chr(10), 1)[1]}", "",
             *rules_section()]

    # 1. Trades the liquidity fix removes, from the previous run (signal-day liquidity, original C10 exit).
    new_mask = ep.prior_liquidity(panel, BASE)
    adv_prior = np.full_like(panel.adv20, np.nan)
    adv_prior[1:] = panel.adv20[:-1]
    close_prior = np.full_like(panel.close_ff, np.nan)
    close_prior[1:] = (panel.close_ff * panel.factor)[:-1]
    window = panel.factor[panel.dates >= "2016-01-01"]
    multiple = np.nanmax(window, axis=0) / np.nanmin(window, axis=0)
    lines += ["## 1. Trades removed by the liquidity fix", "",
              "Previous run: SPY filter, signal-day liquidity, original C10 exit.", "",
              "| Period | Previous trades | Removed | Prior 20-day dollar volume under $20M | Prior close under $5 | "
              "Split adjustment over 100-fold | Removed tickers |", "|---|---|---|---|---|---|---|"]
    removed_info = {}
    for period in TWO:
        rows = list(csv.DictReader(open(OUTPUT / "filter_episodic_pivot_B" / f"trades_{period}.csv", encoding="utf-8")))
        reasons, tickers = Counter(), []
        for r in rows:
            j, t = panel.index[r["ticker"]], panel.day[r["signal_date"]]
            if new_mask[t, j]:
                continue
            tickers.append(f"{r['ticker']} {r['signal_date']}")
            reasons["adv"] += int(not adv_prior[t, j] >= 20e6)
            reasons["price"] += int(not close_prior[t, j] >= 5)
            reasons["split"] += int(not multiple[j] <= 100)
        removed_info[period] = {"previous": len(rows), "removed": len(tickers), "reasons": dict(reasons), "tickers": tickers}
        lines.append(f"| {PERIOD_TITLES[period]} | {len(rows)} | {len(tickers)} | {reasons['adv']} | {reasons['price']} | "
                     f"{reasons['split']} | {', '.join(tickers)} |")
    lines += ["", "A removed trade can fail more than one test.", ""]

    # 2-5. Versions.
    results, stats = {}, {}
    inners, shared = {}, {}
    for code, (label, rule) in EXITS.items():
        inners[code] = ep.EpisodicPivot(dates)
        for mode, (mlabel, rules) in MODES.items():
            name = f"ep_fixed_{code}_{mode}"
            inner = inners[code]
            summary = run_test(panel, name, lambda: mf.Filtered(inner, masks["B"], "B", shared), {**BASE, "exit": rule},
                               bench, rules=rules,
                               description=f"Episodic pivot, 20% gap, SPY above its 50-day SMA, liquidity tested before the signal day; "
                                           f"exit {label}; {mlabel}.")
            results[(code, mode)] = summary["results"]
            stats[(code, mode)] = {p: expectancy(OUTPUT / name, p) for p in (*TWO, "combined")}
            print(f"{name}: done")

    # Breakeven fix: day-3 closes below entry in exit (a).
    s = mf.Filtered(inners["a"], masks["B"], "B", shared)
    s.setup(panel, {**BASE, "exit": EXITS["a"][1]})
    lines += ["## 2. Breakeven fix", "", "| Period | Trades reaching day 3 | Day-3 close above entry (third sold, stop "
              "to breakeven) | Day-3 close at or below entry (no sale, original stop) |", "|---|---|---|---|"]
    for period in TWO:
        trades = simulate(panel, s, *PERIODS[period])["trades"]
        reached = [t for t in trades if t.holding_sessions >= 3 or t.partial_date]
        sold = sum(1 for t in reached if t.partial_date)
        lines.append(f"| {PERIOD_TITLES[period]} | {len(reached)} | {sold} | {len(reached) - sold} |")
    lines.append("")

    lines += ["## 3-5. Results", "", "Expectancy is shown with its standard error. The overlay holds SPY (dividends "
              "reinvested) with all uninvested capital.", ""]
    for period in (*TWO, "combined"):
        spy = results[("a", "alone_0.5")][period]["spy"]
        lines += [f"### {PERIOD_TITLES[period]}", "",
                  "| Exit | Version | Trades | Win rate | Expectancy ± SE | CAGR | Max drawdown | Sharpe | CAGR minus SPY |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for code in EXITS:
            for mode, (mlabel, _) in MODES.items():
                m = results[(code, mode)][period]
                e, se, n = stats[(code, mode)][period]
                lines.append(f"| {code} | {mlabel} | {m['trades']} | {m['win_rate']:.1f}% | {e:+.3f}R ± {se:.3f} | "
                             f"{m['cagr']:+.2f}% | {m['max_drawdown']:.1f}% | {m['sharpe']:.2f} | "
                             f"{m['cagr'] - spy['cagr']:+.2f} |")
        lines += ["", f"SPY with dividends: CAGR {spy['cagr']:+.2f}%, max drawdown {spy['max_drawdown']:.1f}%, "
                      f"Sharpe {spy['sharpe']:.2f}.", ""]

    lines += ["### Overlay return minus SPY with dividends, by year", "",
              "Years 2016–2021 come from the in-sample runs and 2022 on from the out-of-sample runs.", "",
              "| Year | " + " | ".join(f"{c} {m.split('_')[1]}%" for c in EXITS for m in MODES if m != "alone_0.5") + " |",
              "|---|" + "---|" * 9]
    years = sorted(set(results[("a", "overlay_0.5")]["in_sample"]["yearly"]) | set(results[("a", "overlay_0.5")]["out_of_sample"]["yearly"]))
    for y in years:
        period = "in_sample" if y <= "2021" else "out_of_sample"
        cells = []
        for c in EXITS:
            for mode in MODES:
                if mode == "alone_0.5":
                    continue
                m = results[(c, mode)][period]
                cells.append(f"{m['yearly'][y] - m['spy']['yearly'][y]:+.1f}")
        lines.append(f"| {y} | " + " | ".join(cells) + " |")
    lines.append("")

    best = max(EXITS, key=lambda c: stats[(c, "alone_0.5")]["in_sample"][0])
    lines += [f"Best exit by in-sample expectancy: {EXITS[best][0]}, "
              f"{stats[(best, 'alone_0.5')]['in_sample'][0]:+.3f}R in-sample, "
              f"{stats[(best, 'alone_0.5')]['out_of_sample'][0]:+.3f}R out-of-sample.", ""]
    path = OUTPUT / "ep_fixed_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "ep_fixed_report.json").write_text(json.dumps(
        {"removed": removed_info, "stats": {f"{c}|{m}": v for (c, m), v in stats.items()},
         "results": {f"{c}|{m}": {p: {k: v for k, v in r.items() if k != "equity_curve"} for p, r in res.items()}
                     for (c, m), res in results.items()}}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
