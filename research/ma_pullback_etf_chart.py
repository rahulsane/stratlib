"""After-tax growth of $100,000 and drawdowns for run_ma_pullback_etf.py (reads output/ma_pullback_etf/curves_long.csv).

SPY against the rule as specified (third touch) and the any-touch departure; each group's eight versions share its
colour. The third-or-later rule tracks the third-touch lines and is left out. matplotlib comes from the scratch folder
(%TEMP%\\canslim2007\\pylib); it is not a project dependency.
Run: .venv/Scripts/python research/ma_pullback_etf_chart.py
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
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

OUT = Path(__file__).resolve().parent / "output" / "ma_pullback_etf"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
# Categorical slots 1-3 of the reference palette (validated all-pairs: CVD dE 9.2, normal 24.0; the aqua is under 3:1
# on the surface, so each group is labelled directly and named in the legend).
SPY_COLOR = "#2a78d6"
GROUPS = [("third", "Third touch, as specified", "#eb6834"), ("any", "Any touch, a departure", "#1baf7a")]
VARIANTS = [f"{x}-{s}-{t}" for x in ("target", "trail") for s in ("swing", "entry") for t in ("above", "rising")]


def money(v: float) -> str:
    return "\\$" + (f"{v / 1e6:,.2f}M" if v >= 1e6 else f"{v / 1e3:,.0f}k")


def main() -> None:
    with (OUT / "curves_long.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    c = {k: np.array([float(r[k]) for r in rows]) for k in rows[0] if k != "date"}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(11, 8.2), dpi=150, sharex=True,
                                      gridspec_kw={"height_ratios": [3, 1.6], "hspace": 0.14})
    fig.patch.set_facecolor(SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(True, axis="y", color=GRID, linewidth=0.6)
        ax.tick_params(length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    labels = []
    for key, label, color in GROUPS:
        ends = []
        for v in VARIANTS:
            y = c[f"{v}_{key}_after"]
            top.plot(dates, y, color=color, linewidth=1.0, alpha=0.9)
            bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=color, linewidth=0.8, alpha=0.8)
            ends.append(y[-1])
        labels.append((float(np.median(ends)), f"{label}  {money(min(ends))}–{money(max(ends))}", color))
    y = c["spy_after"]
    top.plot(dates, y, color=SPY_COLOR, linewidth=2.0, zorder=5)
    bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=SPY_COLOR, linewidth=1.3, zorder=5)
    labels.append((y[-1], f"SPY  {money(y[-1])}", SPY_COLOR))
    top.set_yscale("log")
    top.yaxis.set_major_locator(FixedLocator([70_000, 100_000, 200_000, 500_000, 1_000_000, 2_000_000, 3_000_000]))
    top.yaxis.set_minor_locator(NullLocator())
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k"))
    top.set_ylim(65_000, 3_500_000)
    placed = []
    for value, text, color in sorted(labels):
        yl = np.log10(value)
        if placed and yl - placed[-1] < 0.07:
            yl = placed[-1] + 0.07
        placed.append(yl)
        top.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                     color=INK, fontsize=9)
        top.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                     arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
    handles = [Line2D([], [], color=SPY_COLOR, linewidth=2.0, label="SPY, bought and held")] + [
        Line2D([], [], color=color, linewidth=1.4, label=f"{label} (8 versions)") for _, label, color in GROUPS]
    top.legend(handles=handles, loc="upper left", frameon=False, fontsize=9, labelcolor=INK)
    bottom.set_title("Drawdown from the previous high", loc="left", color=INK2, fontsize=9.5, pad=4)
    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    bottom.set_yticks([0, -20, -40, -60])
    bottom.set_ylim(-62, 3)
    bottom.xaxis.set_major_locator(mdates.YearLocator(4))
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    first, last = dates[0], dates[-1]
    fig.suptitle(f"MA pullback on SPY and QQQ, after-tax growth of $100,000, {first:%b %Y} to {last:%b %Y} (log scale)",
                 x=0.08, ha="left", color=INK, fontsize=12.5, y=0.975)
    fig.subplots_adjust(left=0.08, right=0.74, top=0.92, bottom=0.07)
    fig.text(0.08, 0.015, "1% risk per trade, no margin. Dividends included; each year's tax (top federal rates, no state) "
             "paid from the account; SPY taxed the same way.", color=INK2, fontsize=8)
    path = OUT / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
