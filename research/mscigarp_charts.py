"""Charts for the MSCI USA Quality GARP Select report (output/mscigarp/*.png). Same style and palette as
garp_charts.py (first three slots of the validated reference palette); matplotlib from the scratch folder."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import garp_charts as GC
from garp_charts import AQUA, BLUE, INK2, ORANGE, plt

OUT = Path(__file__).resolve().parent / "output" / "mscigarp"
GC.OUT = OUT
GC.NAMES.update({"Version 2: ETF (0.20%/yr)": "MSCI GARP", "S&P 500 GARP, ETF (SPGP-style)": "S&P 500 GARP",
                 "MSCI index (official, no costs)": "MSCI index (official)", "Rebuilt index (no costs)": "Rebuild",
                 "QQQ": "QQQ"})


def yearly_bars(results, path):
    rows = results["history"]["Full official history: Dec 2002 - now"]
    g, s = rows["Version 2: ETF (0.20%/yr)"]["yearly"], rows["SPY"]["yearly"]
    years = [y for y in g if "2003" <= y]
    x = np.arange(len(years))
    w = 0.38
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.bar(x - w / 2 - 0.01, [g[y] for y in years], w, color=BLUE, label="MSCI GARP, ETF version")
    ax.bar(x + w / 2 + 0.01, [s[y] for y in years], w, color=AQUA, label="SPY")
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_xticks(x, [y[2:] if y != years[-1] else f"{y[2:]}\nto Sep" for y in years])
    ax.yaxis.set_major_formatter(GC.matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    ax.set_title("Calendar-year total return, 2003-2026", loc="left", fontsize=12, pad=30)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    results = json.load(open(OUT / "results.json"))
    days, cols = GC.read("curves_main.csv")
    GC.growth(days, cols, [("Version 2: ETF (0.20%/yr)", BLUE), ("SPY", AQUA), ("S&P 500 GARP, ETF (SPGP-style)", ORANGE)],
              "Growth of $100,000 in the ETF versions, Dec 2015 - Sep 2026 (log scale)", OUT / "growth_main.png", gap=1.10)
    GC.drawdown(days, cols, [("Version 2: ETF (0.20%/yr)", BLUE), ("SPY", AQUA)],
                "Drawdown from the previous high, Dec 2015 - Sep 2026", OUT / "drawdown_main.png")
    fdays, fcols = GC.read("curves_full.csv")
    GC.growth(fdays, fcols, [("Version 2: ETF (0.20%/yr)", BLUE), ("SPY", AQUA), ("QQQ", ORANGE)],
              "MSCI GARP (ETF version) over MSCI's full history, Dec 2002 - Sep 2026", OUT / "growth_full.png", gap=1.14)
    GC.rolling_excess(fdays, fcols, "Version 2: ETF (0.20%/yr)", "SPY",
                      "Rolling 3-year return of MSCI GARP (ETF version) minus SPY", OUT / "rolling3y_full.png")
    yearly_bars(results, OUT / "yearly_full.png")
    edays, ecols = GC.read("curves_extended.csv")
    GC.growth(edays, ecols, [("MSCI index (official, no costs)", BLUE), ("Rebuilt index (no costs)", ORANGE)],
              "Rebuild against MSCI's official index, Dec 2007 - Sep 2026", OUT / "rebuild_vs_official.png", gap=1.10)
    print("charts written")


if __name__ == "__main__":
    main()
