"""Growth of $100,000 and drawdowns for the Weekend Trend Trader tests (reads output/<folder>/curves_combined.csv).

matplotlib comes from the scratch folder (%TEMP%\\canslim2007\\pylib); it is not a project dependency.
Run: .venv/Scripts/python research/wtt_chart.py [wtt | wtt900]
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.environ.get("TEMP", ""), "canslim2007", "pylib"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

OUTPUT = Path(__file__).resolve().parent / "output"
SURFACE, INK, INK2, GRID, NEUTRAL = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1", "#8a8985"
# Categorical slots 1-3 of the reference palette, in order (validated: adjacent CVD dE 9.2, normal 27.6;
# the green is under 3:1 on the surface, so every series is also labelled directly).
SPY = ("spy_total", "SPY with dividends", "#2a78d6")
ORANGE, GREEN = "#eb6834", "#1baf7a"
STOPS = [("ratchet", "Stop as described", ORANGE), ("recompute", "Recomputed stop", GREEN)]
WTT_NOTE = ("Weekend Trend Trader on {}; 5% per position, at most 20; slippage included, no dividends or interest on "
            "cash. SPY with dividends.")
# Per study: the two random-selection series (only the first gets a percentile band: the bands overlap and blend),
# the dashed fixed-ranking series, and the footnote.
STUDIES = {
    "wtt": {"lines": STOPS, "dashed": ("ratchet_ret63", "Stop as described, ranked by 63-day return", "63-day ranking"),
            "note": WTT_NOTE.format("US common stocks within the liquidity floor")},
    "wtt900": {"lines": STOPS, "dashed": ("ratchet_ret63", "Stop as described, ranked by 63-day return", "63-day ranking"),
               "note": WTT_NOTE.format("S&P 500 + MidCap 400 members (point in time)")},
    "wttqs": {"lines": [("r3000", "Russell 3000", ORANGE), ("sp900", "S&P 500 + MidCap 400", GREEN)],
              "short": {"sp900": "S&P 900"},
              "dashed": ("r3000_ret63", "Russell 3000, ranked by 3-month return", "R3000 3-month"),
              "note": "QuantifiedStrategies-style WTT on index members (point in time): 10 positions of 10%, a 40% stop "
                      "that never tightens, no costs, dividends or interest. SPY with dividends."},
}


def load(out: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    with (out / "curves_combined.csv").open() as f:
        rows = list(csv.DictReader(f))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    return dates, {k: np.array([float(r[k]) for r in rows]) for k in rows[0] if k != "date"}


def spread(values: list[float], gap: float) -> list[float]:
    """Nudge label positions apart (sorted ascending) so none sit closer than gap."""
    order = np.argsort(values)
    out = list(values)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


def main(folder: str = "wtt") -> None:
    out = OUTPUT / folder
    dates, c = load(out)
    study = STUDIES[folder]
    lines = study["lines"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(11, 8.2), dpi=150, sharex=True,
                                      gridspec_kw={"height_ratios": [3, 1.6], "hspace": 0.12})
    fig.patch.set_facecolor(SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(True, axis="y", color=GRID, linewidth=0.6)
        ax.tick_params(length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

    labels = []  # (log10 of the final value, text, colour)
    for key, label, color in lines:
        band = key == lines[0][0]
        if band:
            top.fill_between(dates, c[f"{key}_p5"], c[f"{key}_p95"], color=color, alpha=0.13, linewidth=0)
        top.plot(dates, c[f"{key}_p50"], color=color, linewidth=1.6, solid_capstyle="round",
                 label=f"{label}, random selection: median" + (" and 5th–95th percentile" if band else ""))
        short = study.get("short", {}).get(key, label)
        labels.append((c[f"{key}_p50"][-1], f"{short}  ${c[f'{key}_p50'][-1] / 1e3:,.0f}k", color))
        bottom.plot(dates, c[f"{key}_dd_p50"], color=color, linewidth=1.2, solid_capstyle="round")
    dashed, dashed_label, dashed_short = study["dashed"]
    top.plot(dates, c[dashed], color=NEUTRAL, linewidth=1.1, linestyle=(0, (4, 2)), label=dashed_label)
    labels.append((c[dashed][-1], f"{dashed_short}  ${c[dashed][-1] / 1e3:,.0f}k", NEUTRAL))
    key, label, color = SPY
    top.plot(dates, c[key], color=color, linewidth=1.4, label=label, solid_capstyle="round")
    labels.append((c[key][-1], f"SPY  ${c[key][-1] / 1e3:,.0f}k", color))
    spy_dd = 100 * (c[key] / np.maximum.accumulate(c[key]) - 1)
    bottom.plot(dates, spy_dd, color=color, linewidth=1.1, solid_capstyle="round")

    top.set_yscale("log")
    lo = min(c[k].min() for k in c if not k.startswith(("spy_price",)) and "_dd_" not in k)
    hi = max(c[k].max() for k in c if "_dd_" not in k)
    ticks = [t for t in [10_000, 20_000, 30_000, 50_000, 100_000, 200_000, 300_000, 500_000, 1_000_000]
             if lo * 0.8 <= t <= hi * 1.25]
    top.yaxis.set_major_locator(FixedLocator(ticks))
    top.yaxis.set_minor_locator(NullLocator())
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k"))
    top.set_ylim(lo * 0.8, hi * 1.25)
    finals = spread([np.log10(v) for v, _, _ in labels], 0.045)
    for (value, text, color), yl in zip(labels, finals):
        top.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                     color=INK, fontsize=9)
        top.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                     arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
    first, last = dates[0], dates[-1]
    top.set_title(f"Growth of $100,000, {first.day} {first:%b %Y} to {last.day} {last:%b %Y} (log scale)",
                  loc="left", color=INK, fontsize=12, pad=40)
    leg = top.legend(loc="upper left", bbox_to_anchor=(0, 1.11), ncol=2, frameon=False, fontsize=8.5,
                     handlelength=1.6, columnspacing=1.4)
    for t in leg.get_texts():
        t.set_color(INK2)
    bottom.set_title("Drawdown from the previous high (random selection: median run at each date)", loc="left",
                     color=INK, fontsize=11, pad=6)
    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    floor = min(spy_dd.min(), *(c[f"{k}_dd_p50"].min() for k, _, _ in lines))
    bottom.set_ylim(floor * 1.12, 3)
    bottom.set_yticks([t for t in (0, -10, -20, -30, -40, -50, -60) if t >= floor * 1.12])
    bottom.xaxis.set_major_locator(mdates.YearLocator(2))
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.subplots_adjust(left=0.08, right=0.84, top=0.88, bottom=0.06)
    fig.text(0.08, 0.015, study["note"], color=INK2, fontsize=8)
    path = out / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main(*sys.argv[1:2])
