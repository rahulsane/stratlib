"""Growth of $100,000 and drawdowns for the Turtle backtest (reads output/turtle/curves_full.csv).

matplotlib comes from the scratch folder (%TEMP%\\canslim2007\\pylib); it is not a project dependency.
Run: .venv/Scripts/python research/turtle_chart.py
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

OUT = Path(__file__).resolve().parent / "output" / "turtle"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
# Categorical slots 1-4 of the reference palette, in order (validated: adjacent CVD dE 9.1, normal 22.9).
SERIES = [("SPY", "SPY buy-and-hold", "#2a78d6"), ("QQQ", "QQQ buy-and-hold", "#eb6834"),
          ("S2 as written", "System 2, rules as written", "#1baf7a"),
          ("S2 1x cap", "System 2, no borrowing (1x cap)", "#eda100")]


def load() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    with (OUT / "curves_full.csv").open() as f:
        rows = list(csv.DictReader(f))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    return dates, {k: np.array([float(r[k]) for r in rows]) for k, _, _ in SERIES}


def spread(values: list[float], gap: float) -> list[float]:
    """Nudge label positions apart (sorted ascending) so none sit closer than gap."""
    order = np.argsort(values)
    out = list(values)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


def main() -> None:
    dates, curves = load()
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

    finals = []
    for k, label, color in SERIES:
        y = curves[k]
        top.plot(dates, y, color=color, linewidth=1.1, label=label, solid_capstyle="round")
        dd = 100 * (y / np.maximum.accumulate(y) - 1)
        bottom.plot(dates, dd, color=color, linewidth=1.0, solid_capstyle="round")
        finals.append(np.log10(y[-1]))
    top.set_yscale("log")
    ticks = [50_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000, 5_000_000]
    top.yaxis.set_major_locator(FixedLocator(ticks))
    top.yaxis.set_minor_locator(NullLocator())
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k"))
    top.set_ylim(40_000, 6_500_000)
    # direct labels at the right end, in ink, with a colour swatch carrying identity
    x_end = dates[-1]
    for (k, label, color), yl in zip(SERIES, spread(finals, 0.09)):
        top.annotate(f"{k}  ${curves[k][-1] / 1e6:.2f}M", xy=(x_end, 10 ** yl), xytext=(14, 0),
                     textcoords="offset points", va="center", color=INK, fontsize=9)
        top.annotate("", xy=(x_end, 10 ** yl), xytext=(12, 0), textcoords="offset points",
                     arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
    top.set_title("Growth of $100,000, 1 Aug 2006 to 30 Sep 2026 (log scale)", loc="left", color=INK,
                  fontsize=12, pad=26)
    leg = top.legend(loc="upper left", bbox_to_anchor=(0, 1.07), ncol=4, frameon=False, fontsize=9,
                     handlelength=1.6, columnspacing=1.4)
    for t in leg.get_texts():
        t.set_color(INK2)
    bottom.set_title("Drawdown from the previous high", loc="left", color=INK, fontsize=11, pad=6)
    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    bottom.set_ylim(-90, 3)
    bottom.set_yticks([0, -25, -50, -75])
    bottom.xaxis.set_major_locator(mdates.YearLocator(2))
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.subplots_adjust(left=0.08, right=0.86, top=0.9, bottom=0.06)
    fig.text(0.08, 0.015, "System 2: 55-day breakouts, 20-day exits, 1% of equity per N, six ETFs, long and short; "
             "costs, T-bill interest and margin interest included. Benchmarks with dividends.",
             color=INK2, fontsize=8)
    path = OUT / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
