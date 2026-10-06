"""Chart: cumulative return vs SPY after earnings-gap signals (reads output/ep_drift_by_day.csv).

matplotlib is not a project dependency; this uses the copy in the scratch folder.
Run: .venv\\Scripts\\python research\\chart_ep_drift.py
"""

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("TEMP", ""), "canslim2007", "pylib"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUT = Path(__file__).resolve().parent / "output"
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
SERIES = {5: ("#2a78d6", "Gap ≥5%"), 10: ("#eb6834", "Gap ≥10%"), 20: ("#1baf7a", "Gap ≥20%")}  # slots 1-3

data = defaultdict(dict)
counts = {}
for row in csv.DictReader(open(OUT / "ep_drift_by_day.csv", encoding="utf-8")):
    if row["neglected"] != "no":
        continue
    key = (int(row["gap_pct"]), row["group"])
    data[key][int(row["day"])] = (float(row["mean_vs_spy_pct"]), float(row["median_vs_spy_pct"]))
    counts[key] = int(row["signals"])

fig, axes = plt.subplots(2, 2, figsize=(11, 7.2), sharex=True, sharey="row", facecolor=SURFACE)
labels = []  # (axis, [[value, label y, text], ...]) placed after the y limits are final
periods = (("in_sample", "In-sample, 2016–2021"), ("out_of_sample", "Out-of-sample, 2022 on"))
for r, (stat, label) in enumerate(((0, "Mean"), (1, "Median"))):
    for c, (group, title) in enumerate(periods):
        ax = axes[r][c]
        ax.set_facecolor(SURFACE)
        ax.axhline(0, color=INK_2, linewidth=1)
        ends = []
        for gap, (color, name) in SERIES.items():
            series = data[(gap, group)]
            days = [0] + sorted(series)
            values = [0.0] + [series[d][stat] for d in sorted(series)]
            ax.plot(days, values, color=color, linewidth=2, solid_capstyle="round")
            ends.append([values[-1], values[-1], f"{name}  {values[-1]:+.1f}%"])
        labels.append((ax, ends))
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=INK_2, labelsize=8.5)
        ax.set_xlim(0, 60)
        ax.set_xticks([0, 10, 20, 30, 40, 50, 60])
        if r == 0:
            n = ", ".join(f"{counts[(g, group)]:,}" for g in SERIES)
            ax.set_title(f"{title}\n(signals: {n})", fontsize=10, color=INK, loc="left")
        if c == 0:
            ax.set_ylabel(f"{label} return vs SPY (%)", fontsize=9, color=INK_2)
        if r == 1:
            ax.set_xlabel("Sessions after the signal day's close", fontsize=9, color=INK_2)
# Direct labels at the line ends, pushed apart so they never overlap (at least 7% of the axis height).
for ax, ends in labels:
    low, high = ax.get_ylim()
    gap = 0.07 * (high - low)
    ends.sort(key=lambda e: e[0])
    for k in range(1, len(ends)):
        ends[k][1] = max(ends[k][1], ends[k - 1][1] + gap)
    shift = max(0.0, ends[-1][1] - high)
    for e in ends:
        ax.annotate(e[2], xy=(60, e[0]), xytext=(62, e[1] - shift), textcoords="data", va="center",
                    fontsize=8.5, color=INK, annotation_clip=False)
handles = [plt.Line2D([], [], color=color, linewidth=2) for color, _ in SERIES.values()]
fig.legend(handles, [name for _, name in SERIES.values()], loc="upper right", frameon=False, fontsize=9,
           labelcolor=INK, ncol=3, bbox_to_anchor=(0.985, 0.995))
fig.suptitle("Return vs SPY after an earnings gap on 3x volume, by gap size", x=0.01, ha="left",
             fontsize=12, color=INK)
fig.tight_layout(rect=(0, 0, 0.93, 0.96))
fig.subplots_adjust(right=0.86, wspace=0.34)
path = OUT / "ep_drift.png"
fig.savefig(path, dpi=150, facecolor=SURFACE)
print(path)
