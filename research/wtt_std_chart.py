"""After-tax growth of $100,000 and drawdowns for run_wtt_standard.py (reads output/wtt_std/curves_combined.csv).

matplotlib comes from the scratch folder (%TEMP%\\canslim2007\\pylib); it is not a project dependency.
Run: .venv/Scripts/python research/wtt_std_chart.py
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

OUT = Path(__file__).resolve().parent / "output" / "wtt_std"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
# Categorical slots 1-3 of the reference palette (validated: adjacent CVD dE 9.2, normal 27.6; the green is under
# 3:1 on the surface, so every line is also labelled directly).
SERIES = [("spy_after", "SPY", "#2a78d6"), ("wtt_{u}_after", "WTT", "#eb6834"), ("qs_{u}_after", "QS-style", "#1baf7a")]
UNIVERSES = [("r3000", "Russell 3000"), ("sp900", "S&P 500 + MidCap 400")]


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


def main() -> None:
    dates, c = load()
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.2), dpi=150, sharex=True, sharey="row",
                             gridspec_kw={"height_ratios": [3, 1.6], "hspace": 0.14, "wspace": 0.32})
    fig.patch.set_facecolor(SURFACE)
    curves = [c[k.format(u=u)] for u, _ in UNIVERSES for k, _, _ in SERIES]
    lo, hi = min(x.min() for x in curves), max(x.max() for x in curves)
    for col, (u, name) in enumerate(UNIVERSES):
        top, bottom = axes[0, col], axes[1, col]
        for ax in (top, bottom):
            ax.set_facecolor(SURFACE)
            ax.grid(True, axis="y", color=GRID, linewidth=0.6)
            ax.tick_params(length=0)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        labels = []
        for key, label, color in SERIES:
            y = c[key.format(u=u)]
            top.plot(dates, y, color=color, linewidth=1.5, solid_capstyle="round", label=label)
            bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=color, linewidth=1.0)
            labels.append((y[-1], f"{label}  ${y[-1] / 1e3:,.0f}k", color))
        top.set_yscale("log")
        ticks = [t for t in [50_000, 70_000, 100_000, 150_000, 200_000, 300_000, 500_000] if lo * 0.85 <= t <= hi * 1.2]
        top.yaxis.set_major_locator(FixedLocator(ticks))
        top.yaxis.set_minor_locator(NullLocator())
        top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e3:g}k"))
        top.set_ylim(lo * 0.85, hi * 1.2)
        for (value, text, color), yl in zip(labels, spread([np.log10(v) for v, _, _ in labels], 0.05)):
            top.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                         color=INK, fontsize=9)
            top.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                         arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
        top.set_title(name, loc="left", color=INK, fontsize=11.5, pad=8)
        bottom.set_title("Drawdown from the previous high", loc="left", color=INK2, fontsize=9.5, pad=4)
        bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
        bottom.set_yticks([0, -20, -40, -60])
        bottom.set_ylim(-68, 3)
        bottom.xaxis.set_major_locator(mdates.YearLocator(2))
        bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    first, last = dates[0], dates[-1]
    fig.suptitle(f"After-tax growth of $100,000, {first.day} {first:%b %Y} to {last.day} {last:%b %Y} (log scale)",
                 x=0.06, ha="left", color=INK, fontsize=12.5, y=0.975)
    fig.subplots_adjust(left=0.06, right=0.88, top=0.9, bottom=0.07)
    fig.text(0.06, 0.015, "Ranked by 12-month momentum. Dividends included; each year's tax (top federal rates, no "
             "state) paid from the account. SPY taxed the same way. Open positions not sold at the end.",
             color=INK2, fontsize=8)
    path = OUT / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
