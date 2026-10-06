"""Charts for the S&P 500 GARP report (output/garp/*.png), drawn from output/garp/results.json and curves_*.csv.

matplotlib comes from the scratch folder (%TEMP%/canslim2007/pylib); it is not a project dependency.
Colours: the first three slots of the validated reference palette (blue, orange, aqua), light surface.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, "C:/Users/rahul/AppData/Local/Temp/canslim2007/pylib")
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

OUT = Path(__file__).resolve().parent / "output" / "garp"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
NAMES = {"Version 2: ETF (0.35%/yr)": "GARP, ETF version", "Version 1: self-managed": "GARP, self-managed",
         "SPY": "SPY", "RSP": "RSP (equal-weight S&P 500)"}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "legend.frameon": False, "lines.linewidth": 2.0,
})


def read(name):
    rows = list(csv.reader(open(OUT / name)))
    head, body = rows[0], rows[1:]
    days = [date.fromisoformat(r[0]) for r in body]
    cols = {h: np.array([float(r[k]) for r in body]) for k, h in enumerate(head) if k}
    return days, cols


def label_end(ax, x, y, text, color_offset=0):
    ax.annotate(text, (x, y), xytext=(6, color_offset), textcoords="offset points", va="center", color=INK2,
                fontsize=9)


def growth(days, cols, series, title, path, log=True, gap=1.08):
    fig, ax = plt.subplots(figsize=(9, 4.8))
    offsets = [0, 0, 0]
    ends = sorted(((cols[s][-1], s) for s, _ in series))
    for (s, color), off in zip(series, offsets):
        ax.plot(days, cols[s], color=color, label=NAMES.get(s, s))
    last = None
    for v, s in ends:
        y = v
        if last is not None and y / last < gap:
            y = last * gap
        money = f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1000:,.0f}k"
        label_end(ax, days[-1], y, f"{NAMES.get(s, s).split(' (')[0]}  {money}")
        last = y
    if log:
        ax.set_yscale("log")
        lo = min(float(cols[s].min()) for s, _ in series)
        hi = max(float(cols[s].max()) for s, _ in series)
        ticks = [t for t in (25e3, 50e3, 75e3, 100e3, 150e3, 200e3, 300e3, 400e3, 600e3, 800e3, 1.2e6, 1.6e6,
                             2.4e6, 3.2e6) if lo * 0.9 <= t <= hi * 1.1]
        ax.set_yticks(ticks)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1000:,.0f}k"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12)
    ax.legend(loc="upper left")
    ax.margins(x=0)
    ax.set_xlim(days[0], days[-1])
    fig.subplots_adjust(right=0.78)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def drawdown(days, cols, series, title, path):
    fig, ax = plt.subplots(figsize=(9, 3.6))
    for s, color in series:
        v = cols[s]
        dd = 100 * (v / np.maximum.accumulate(v) - 1)
        ax.plot(days, dd, color=color, label=NAMES.get(s, s), linewidth=1.6)
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title(title, loc="left", fontsize=12, pad=12)
    ax.legend(loc="lower left")
    ax.set_xlim(days[0], days[-1])
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def rolling_excess(days, cols, a, b, title, path, window=756):
    va, vb = cols[a], cols[b]
    x, y = [], []
    for k in range(window, len(days)):
        ya = (va[k] / va[k - window]) ** (252 / window) - 1
        yb = (vb[k] / vb[k - window]) ** (252 / window) - 1
        x.append(days[k])
        y.append(100 * (ya - yb))
    y = np.array(y)
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(x, y, color=BLUE)
    ax.fill_between(x, 0, y, where=y < 0, color=BLUE, alpha=0.10, linewidth=0)
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
    ax.set_ylabel("percentage points a year")
    ax.set_title(title, loc="left", fontsize=12, pad=12)
    ax.set_xlim(x[0], x[-1])
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def yearly_bars(results, path):
    rows = results["periods"]["Main: Dec 2015 - now"]
    g = rows["Version 2: ETF (0.35%/yr)"]["yearly"]
    s = rows["SPY"]["yearly"]
    years = [y for y in g if y >= "2016"]
    x = np.arange(len(years))
    w = 0.38
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(x - w / 2 - 0.01, [g[y] for y in years], w, color=BLUE, label="GARP, ETF version")
    ax.bar(x + w / 2 + 0.01, [s[y] for y in years], w, color=AQUA, label="SPY")
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_xticks(x, [y if y != years[-1] else f"{y}\n(to Sep)" for y in years])
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title("Calendar-year total return", loc="left", fontsize=12, pad=12)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2)
    ax.set_title("Calendar-year total return", loc="left", fontsize=12, pad=30)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    results = json.load(open(OUT / "results.json"))
    days, cols = read("curves_main.csv")
    growth(days, cols, [("Version 2: ETF (0.35%/yr)", BLUE), ("SPY", AQUA), ("RSP", ORANGE)],
           "Growth of $100,000, Dec 2015 - Sep 2026 (log scale, total return)", OUT / "growth_main.png")
    drawdown(days, cols, [("Version 2: ETF (0.35%/yr)", BLUE), ("SPY", AQUA)],
             "Drawdown from the previous high", OUT / "drawdown_main.png")
    rolling_excess(days, cols, "Version 2: ETF (0.35%/yr)", "SPY",
                   "Rolling 3-year return of GARP (ETF version) minus SPY", OUT / "rolling3y_main.png")
    yearly_bars(results, OUT / "yearly_main.png")
    edays, ecols = read("curves_extended.csv")
    growth(edays, ecols, [("Version 2: ETF (0.35%/yr)", BLUE), ("SPY", AQUA), ("RSP", ORANGE)],
           "Extended (approximate), Dec 2007 - Sep 2026: growth of $100,000", OUT / "growth_extended.png",
           gap=1.16)
    print("charts written")


if __name__ == "__main__":
    main()
