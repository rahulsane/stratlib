"""After-tax growth of $100,000 and drawdowns for run_ma_pullback.py (reads output/ma_pullback/curves_combined.csv).

The four versions with each exit share that exit's colour: the chart compares the exits with SPY; the tables in the
report separate the versions. matplotlib comes from the scratch folder (%TEMP%\\canslim2007\\pylib); it is not a
project dependency.
Run: .venv/Scripts/python research/ma_pullback_chart.py
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

OUT = Path(__file__).resolve().parent / "output" / "ma_pullback"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
# Categorical slots 1-3 of the reference palette (validated all-pairs: CVD dE 9.2, normal 24.0; the aqua is under 3:1
# on the surface, so every group is also labelled directly and named in the legend).
SPY_COLOR = "#2a78d6"
GROUPS = [("target", "Target", "#eb6834"), ("trail", "EMA exit", "#1baf7a")]
UNIVERSES = [("sp900", "S&P 500 + MidCap 400"), ("r3000", "Russell 3000")]
VARIANTS = [f"{x}-{s}-{t}" for x in ("target", "trail") for s in ("swing", "entry") for t in ("above", "rising")]


def load() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    with (OUT / "curves_combined.csv").open() as f:
        rows = list(csv.DictReader(f))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    return dates, {k: np.array([float(r[k]) for r in rows]) for k in rows[0] if k != "date"}


def spread(values: list[float], gap: float) -> list[float]:
    order = np.argsort(values)
    out = list(values)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


def money(v: float) -> str:
    return "\\$" + f"{v / 1e3:,.0f}k"     # escaped: two dollar signs would start matplotlib's math text


def main() -> None:
    dates, c = load()
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.2), dpi=150, sharex=True, sharey="row",
                             gridspec_kw={"height_ratios": [3, 1.6], "hspace": 0.14, "wspace": 0.42})
    fig.patch.set_facecolor(SURFACE)
    curves = [c["spy_after"]] + [c[f"{v}_{u}_after"] for u, _ in UNIVERSES for v in VARIANTS]
    lo, hi = min(x.min() for x in curves), max(x.max() for x in curves)
    dd_floor = min(100 * (x / np.maximum.accumulate(x) - 1).min() for x in curves)
    for col, (u, name) in enumerate(UNIVERSES):
        top, bottom = axes[0, col], axes[1, col]
        for ax in (top, bottom):
            ax.set_facecolor(SURFACE)
            ax.grid(True, axis="y", color=GRID, linewidth=0.6)
            ax.tick_params(length=0)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        labels = []
        for key, label, color in GROUPS:
            ends = []
            for v in [v for v in VARIANTS if v.startswith(key)]:
                y = c[f"{v}_{u}_after"]
                top.plot(dates, y, color=color, linewidth=1.1, alpha=0.9, solid_capstyle="round")
                bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=color, linewidth=0.8, alpha=0.8)
                ends.append(y[-1])
            labels.append((float(np.median(ends)), f"{label}  {money(min(ends))}–{money(max(ends))}", color))
        y = c["spy_after"]
        top.plot(dates, y, color=SPY_COLOR, linewidth=2.0, solid_capstyle="round", zorder=5)
        bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=SPY_COLOR, linewidth=1.3, zorder=5)
        labels.append((y[-1], f"SPY  {money(y[-1])}", SPY_COLOR))
        top.set_yscale("log")
        ticks = [t for t in [30_000, 50_000, 70_000, 100_000, 150_000, 200_000, 300_000, 500_000]
                 if lo * 0.85 <= t <= hi * 1.2]
        top.yaxis.set_major_locator(FixedLocator(ticks))
        top.yaxis.set_minor_locator(NullLocator())
        top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e3:g}k"))
        top.set_ylim(lo * 0.85, hi * 1.2)
        for (value, text, color), yl in zip(labels, spread([np.log10(v) for v, _, _ in labels], 0.06)):
            top.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                         color=INK, fontsize=9)
            top.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                         arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
        top.set_title(name, loc="left", color=INK, fontsize=11.5, pad=8)
        bottom.set_title("Drawdown from the previous high", loc="left", color=INK2, fontsize=9.5, pad=4)
        bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
        step = 20 if dd_floor > -80 else 25
        bottom.set_yticks(list(range(0, int(dd_floor) - step, -step)))
        bottom.set_ylim(dd_floor - 5, 3)
        bottom.xaxis.set_major_locator(mdates.YearLocator(2))
        bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    handles = [Line2D([], [], color=SPY_COLOR, linewidth=2.0, label="SPY")] + [
        Line2D([], [], color=color, linewidth=1.4, label=name)
        for (_, _, color), name in zip(GROUPS, ["Target before the swing high (4 versions)",
                                                "Close below the 50-day EMA (4 versions)"])]
    axes[0, 0].legend(handles=handles, loc="upper left", frameon=False, fontsize=8.5, labelcolor=INK,
                      title="Exit rule; each has two stops × two trend filters", title_fontsize=8.5,
                      alignment="left")
    first, last = dates[0], dates[-1]
    fig.suptitle(f"MA pullback, after-tax growth of $100,000, {first.day} {first:%b %Y} to {last.day} {last:%b %Y} "
                 "(log scale)", x=0.06, ha="left", color=INK, fontsize=12.5, y=0.975)
    fig.subplots_adjust(left=0.06, right=0.86, top=0.9, bottom=0.07)
    fig.text(0.06, 0.015, "Ranked by 12-month momentum. Dividends included; each year's tax (top federal rates, no "
             "state) paid from the account. SPY taxed the same way. Open positions not sold at the end.",
             color=INK2, fontsize=8)
    path = OUT / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
