"""Growth of $100,000 and drawdowns, 1950-2026, for run_levtrend.py (reads output/levtrend/curves_full.csv).

matplotlib comes from the scratch folder (%TEMP%\\canslim2007\\pylib), or from PYTHONPATH; it is not a project
dependency.
Run: .venv/Scripts/python research/levtrend_chart.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.append(os.path.join(os.environ.get("TEMP", ""), "canslim2007", "pylib"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

OUT = Path(__file__).resolve().parent / "output" / "levtrend"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
# Categorical slots 1-3 of the reference palette (validated all-pairs: CVD dE 9.2, normal 24.0; the aqua is under
# 3:1 on the surface, so every line is also labelled directly).
SERIES = [("SPY", "SPY buy-and-hold", "#2a78d6"),
          ("2x, month-end check", "2x, monthly signal", "#eb6834"),
          ("SPY at the same volatility as 2x month-end, through the funds", "Vol-matched SPY, no timing", "#1baf7a")]


def load() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    with (OUT / "curves_full.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    dates = np.array([date.fromisoformat(r["date"]) for r in rows])
    return dates, {k: np.array([float(r[k]) for r in rows]) for k in rows[0] if k != "date"}


def money(v: float) -> str:
    return f"${v / 1e9:,.1f}B" if v >= 1e9 else f"${v / 1e6:,.0f}M" if v >= 1e6 else f"${v / 1e3:,.0f}k"


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
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(12, 8.2), dpi=150, sharex=True,
                                      gridspec_kw={"height_ratios": [3, 1.5], "hspace": 0.12})
    fig.patch.set_facecolor(SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(True, axis="y", color=GRID, linewidth=0.6)
        ax.tick_params(length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    labels = []
    for key, label, color in SERIES:
        y = c[key]
        top.plot(dates, y, color=color, linewidth=1.5, solid_capstyle="round", label=label)
        bottom.plot(dates, 100 * (y / np.maximum.accumulate(y) - 1), color=color, linewidth=0.8)
        labels.append((y[-1], f"{label}  {money(y[-1])}", color))
    top.set_yscale("log")
    lo, hi = min(c[k].min() for k, _, _ in SERIES), max(c[k].max() for k, _, _ in SERIES)
    top.set_ylim(lo * 0.7, hi * 1.6)
    top.yaxis.set_major_locator(FixedLocator([10 ** e for e in range(5, 11) if lo * 0.7 <= 10 ** e <= hi * 1.6]))
    top.yaxis.set_minor_locator(NullLocator())
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v / 1e9:g}B" if v >= 1e9 else
                                                f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}k"))
    for (value, text, color), yl in zip(labels, spread([np.log10(v) for v, _, _ in labels], 0.22)):
        top.annotate(text, xy=(dates[-1], 10 ** yl), xytext=(14, 0), textcoords="offset points", va="center",
                     color=INK, fontsize=9)
        top.annotate("", xy=(dates[-1], 10 ** yl), xytext=(12, 0), textcoords="offset points",
                     arrowprops={"arrowstyle": "-", "color": color, "linewidth": 2.2, "shrinkA": 0, "shrinkB": 0})
    bottom.set_title("Drawdown from the previous high", loc="left", color=INK2, fontsize=9.5, pad=4)
    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    bottom.set_yticks([0, -20, -40, -60])
    bottom.set_ylim(-75, 3)
    bottom.xaxis.set_major_locator(mdates.YearLocator(10))
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    first, last = dates[0], dates[-1]
    fig.suptitle(f"Growth of $100,000, {first.day} {first:%b %Y} to {last.day} {last:%b %Y} (log scale)",
                 x=0.07, ha="left", color=INK, fontsize=12.5, y=0.975)
    fig.subplots_adjust(left=0.07, right=0.73, top=0.92, bottom=0.08)
    lev = json.loads((OUT / "results.json").read_text(encoding="utf-8"))["standard"]["full"]["strategies"][
        "2x, month-end check"]["same_vol_funds"]["leverage"]
    fig.text(0.07, 0.02, "Pre-tax, actual ETF costs. Pre-1993 SPY: index total return less a 0.09% expense ratio. "
             f"Vol-matched SPY: constant {lev:.2f}x via SPY and SSO, no timing.",
             color=INK2, fontsize=8)
    path = OUT / "equity_drawdown.png"
    fig.savefig(path, facecolor=SURFACE)
    print(path)


if __name__ == "__main__":
    main()
