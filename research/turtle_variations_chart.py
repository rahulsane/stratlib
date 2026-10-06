"""Growth of $100,000 for the two Turtle variations at each leverage setting (reads
output/turtle_variations/curves_full.csv). matplotlib comes from %TEMP%\\canslim2007\\pylib.
Run: .venv/Scripts/python research/turtle_variations_chart.py
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

from turtle_chart import GRID, INK, INK2, SURFACE, spread  # noqa: E402

OUT = Path(__file__).resolve().parent / "output" / "turtle_variations"
# Categorical slots 1-4 of the reference palette, in order (validated for adjacent pairs).
SERIES = [("SPY", "SPY", "SPY buy-and-hold", "#2a78d6"), ("QQQ", "QQQ", "QQQ buy-and-hold", "#eb6834"),
          ("v1 S2", "No SPY/TLT", "System 2 without SPY and TLT, long and short", "#1baf7a"),
          ("v2 S2", "Long only", "System 2 long only, all six", "#eda100")]
PANELS = [("as written", "Rules as written (futures-style leverage)"), ("2x cap", "Gross exposure capped at 2x equity"),
          ("1x cap", "Gross exposure capped at 1x equity (no borrowing)")]


def column(code: str, lev: str) -> str:
    return code if code in ("SPY", "QQQ") else f"{code} {lev}"


def main() -> None:
    with (OUT / "curves_full.csv").open() as f:
        rows = list(csv.DictReader(f))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "xtick.color": INK2, "ytick.color": INK2})
    fig, axes = plt.subplots(3, 1, figsize=(11, 12), dpi=150, sharex=True, sharey=True,
                             gridspec_kw={"hspace": 0.28})
    fig.patch.set_facecolor(SURFACE)
    ticks = [50_000, 100_000, 300_000, 1_000_000, 3_000_000, 10_000_000, 30_000_000]
    for ax, (lev, title) in zip(axes, PANELS):
        ax.set_facecolor(SURFACE)
        ax.grid(True, axis="y", color=GRID, linewidth=0.6)
        ax.tick_params(length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        finals, values = [], []
        for code, short, label, color in SERIES:
            y = np.array([float(r[column(code, lev)]) for r in rows])
            ax.plot(dates, y, color=color, linewidth=1.1, label=label, solid_capstyle="round")
            finals.append(np.log10(y[-1]))
            values.append(y[-1])
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(FixedLocator(ticks))
        ax.yaxis.set_minor_locator(NullLocator())
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k"))
        ax.set_ylim(35_000, 60_000_000)
        for (code, short, _, color), yl, v in zip(SERIES, spread(finals, 0.16), values):
            text = f"{short}  ${v / 1e6:.2f}M" if v >= 1e6 else f"{short}  ${v / 1e3:.0f}k"
            ax.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                        color=INK, fontsize=9)
            ax.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                        arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
        ax.set_title(title, loc="left", color=INK, fontsize=11, pad=6)
    axes[-1].xaxis.set_major_locator(mdates.YearLocator(2))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.suptitle("Growth of $100,000, 1 Aug 2006 to 30 Sep 2026 (log scale, same scale in each panel)",
                 x=0.08, ha="left", color=INK, fontsize=12, y=0.975)
    leg = axes[0].legend(loc="upper left", bbox_to_anchor=(0, 1.2), ncol=4, frameon=False, fontsize=9,
                         handlelength=1.6, columnspacing=1.2)
    for t in leg.get_texts():
        t.set_color(INK2)
    fig.subplots_adjust(left=0.08, right=0.84, top=0.92, bottom=0.05)
    fig.text(0.08, 0.015, "Both variations were chosen after the first report's results (hindsight). Costs, T-bill "
             "interest and margin interest included; benchmarks with dividends.", color=INK2, fontsize=8)
    path = OUT / "growth_by_leverage.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
