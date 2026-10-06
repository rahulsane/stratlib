"""Episodic pivot with the SPY-above-50-day filter (20% gap, C10 exit, 0.5% risk): detailed report.

1-2. Base figures and CAGR relative to exposure.  3. Idle cash earning T-bills.
4. SPY overlay vs SPY buy-and-hold.  5. Ten random trades with their daily bars.
6. Average returns after every signal (traded or not) vs SPY.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
import market_filters as mf  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, run_test, simulate  # noqa: E402
from lab import load_all  # noqa: E402
from run_ep import _ffill_2d  # noqa: E402
from strategies import episodic_pivot as ep  # noqa: E402

PARAMS = {**ep.DEFAULTS, "gap_pct": 20, "neglected": False, "exit": ep.C10, "market_filter": "B"}
HORIZONS = (1, 5, 10, 20, 60)
SEED = 2026


def main() -> None:
    panel, bench = load_all()
    dates = earnings.load(panel)
    masks = mf.compute(panel)["masks"]
    inner, shared = ep.EpisodicPivot(dates), {}
    factory = lambda: mf.Filtered(inner, masks["B"], "B", shared)  # noqa: E731
    desc = "Episodic pivot, 20% gap, C10 exit, SPY above its 50-day SMA on the signal day"
    runs = {
        "base": run_test(panel, "filter_episodic_pivot_B", factory, PARAMS, bench,
                         description=f"{desc} (market filter B)."),
        "tbill": run_test(panel, "filter_episodic_pivot_B_tbill", factory, PARAMS, bench,
                          rules=Rules(idle_cash_tbill=True), description=f"{desc}; idle cash earns the 3-month T-bill rate."),
        "overlay": run_test(panel, "filter_episodic_pivot_B_overlay", factory, PARAMS, bench,
                            rules=Rules(overlay_spy=True), description=f"{desc}; uninvested capital held in SPY."),
    }
    res = {k: v["results"] for k, v in runs.items()}
    periods = ("in_sample", "out_of_sample")
    lines = ["# Episodic pivot with the SPY filter: detailed report", "",
             f"{desc}; 0.5% risk per trade. Data through {panel.dates[-1]}.", ""]

    # 1-3
    lines += ["## 1-3. Trades, exposure, return on invested capital, T-bill cash", "",
              "| | " + " | ".join(PERIOD_TITLES[p] for p in periods) + " |", "|---|" + "---|" * len(periods)]
    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(p) for p in periods) + " |")
    b, t = res["base"], res["tbill"]
    row("Trades", lambda p: f"{b[p]['trades']}")
    row("Win rate", lambda p: _f(b[p]["win_rate"], 1, "%"))
    row("Expectancy", lambda p: _f(b[p]["expectancy_r"], 3, "R"))
    row("Days with at least one open position", lambda p: _f(b[p]["exposure_time_pct"], 1, "%"))
    row("Average share of capital invested", lambda p: _f(b[p]["exposure_avg_invested_pct"], 1, "%"))
    row("CAGR (idle cash earns nothing)", lambda p: _f(b[p]["cagr"], 2, "%"))
    row("CAGR / days with a position", lambda p: _f(100 * b[p]["cagr"] / b[p]["exposure_time_pct"], 1, "%"))
    row("CAGR / average capital invested", lambda p: _f(100 * b[p]["cagr"] / b[p]["exposure_avg_invested_pct"], 1, "%"))
    row("Max drawdown", lambda p: _f(b[p]["max_drawdown"], 1, "%"))
    row("Sharpe", lambda p: _f(b[p]["sharpe"], 2))
    row("CAGR, idle cash in T-bills", lambda p: _f(t[p]["cagr"], 2, "%"))
    row("Max drawdown, idle cash in T-bills", lambda p: _f(t[p]["max_drawdown"], 1, "%"))
    row("Sharpe, idle cash in T-bills", lambda p: _f(t[p]["sharpe"], 2))
    row("Interest earned", lambda p: f"${t[p]['counts'].get('interest_earned', 0):,.0f}")
    row("Average 3-month T-bill yield", lambda p: _f(avg_tbill(panel, bench, *PERIODS[p]), 2, "%"))
    lines.append("")

    # 4
    o = res["overlay"]
    lines += ["## 4. SPY overlay vs SPY buy-and-hold", "",
              "| | Overlay: CAGR / max DD / Sharpe | SPY with dividends: CAGR / max DD / Sharpe |", "|---|---|---|"]
    for p in (*periods, "combined"):
        s = o[p]["spy"]
        lines.append(f"| {PERIOD_TITLES[p]} | {o[p]['cagr']:+.2f}% / {o[p]['max_drawdown']:.1f}% / {o[p]['sharpe']:.2f} | "
                     f"{s['cagr']:+.2f}% / {s['max_drawdown']:.1f}% / {s['sharpe']:.2f} |")
    lines += ["", "| Year | Overlay | SPY with dividends |", "|---|---|---|"]
    for y, v in o["combined"]["yearly"].items():
        lines.append(f"| {y} | {v:+.1f}% | {o['combined']['spy']['yearly'][y]:+.1f}% |")
    lines.append("")

    # 5
    lines += ["## 5. Ten random trades (five per period) with daily bars", "",
              "Prices are split-adjusted to today (as most charts show them); \"as traded\" is noted where a later "
              "split changed the basis. Entry, stop and exit prices include slippage.", ""]
    rng = np.random.default_rng(SEED)
    for p in periods:
        wrapper = factory()
        wrapper.setup(panel, PARAMS)
        trades = simulate(panel, wrapper, *PERIODS[p])["trades"]
        for k in sorted(rng.choice(len(trades), 5, replace=False)):
            lines += trade_card(panel, trades[k], dates, p)

    # 6
    spy_j = panel.index["SPY"]
    em = ep.earnings_mask(panel, dates)
    signal = ep.signal_mask(panel, PARAMS, em) & panel.eligible & masks["B"][:, None]
    signal[panel.dates < "2016-01-01"] = False
    strong = ep.strong_close(panel, PARAMS["close_in_range"])
    held = _ffill_2d(np.where(panel.valid, panel.close, np.nan))
    lines += ["## 6. Average return after every signal (traded or not) vs SPY", "",
              "Signals: gap of 20% or more, volume at least 3x the 50-day average, first session after an earnings "
              "release, SPY above its 50-day SMA, liquid stock. Returns run from the signal day's close; a delisted "
              "stock is held at its last close. Each horizon uses the signals with that many later sessions.", "",
              "| Period | Signals | Horizon | Stock mean | SPY mean | Difference | Median difference | Beat SPY |",
              "|---|---|---|---|---|---|---|---|"]
    drift = {}
    for p in periods:
        start, end = PERIODS[p]
        for label, extra in (("all signals", None), ("entry condition met", strong)):
            sig = signal.copy()
            sig[(panel.dates < start) | (panel.dates > (end or "9999"))] = False
            if extra is not None:
                sig &= extra
            tt, jj = np.nonzero(sig)
            for h in HORIZONS:
                ok = tt + h < len(panel.dates)
                a, c = tt[ok], jj[ok]
                stock = held[a + h, c] / panel.close[a, c] - 1
                spy = held[a + h, spy_j] / held[a, spy_j] - 1
                diff = stock - spy
                drift[(p, label, h)] = {"n": int(ok.sum()), "stock": float(stock.mean()), "spy": float(spy.mean()),
                                        "diff": float(diff.mean()), "median_diff": float(np.median(diff)),
                                        "beat": float((diff > 0).mean())}
                d = drift[(p, label, h)]
                lines.append(f"| {PERIOD_TITLES[p]}, {label} | {d['n']} | {h} | {100 * d['stock']:+.2f}% | "
                             f"{100 * d['spy']:+.2f}% | {100 * d['diff']:+.2f}% | {100 * d['median_diff']:+.2f}% | "
                             f"{100 * d['beat']:.0f}% |")
    path = OUTPUT / "ep_filterB_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "ep_filterB_report.json").write_text(json.dumps(
        {"results": {k: {p: {kk: vv for kk, vv in m.items() if kk != "equity_curve"} for p, m in r.items()}
                     for k, r in res.items()},
         "drift": {f"{p}|{l}|{h}": v for (p, l, h), v in drift.items()}}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


def avg_tbill(panel, bench, start, end) -> float:
    vals = [v for d, v in bench["tbill3m"].items() if d >= start and d <= (end or "9999")]
    return float(np.mean(vals)) if vals else math.nan


def trade_card(panel, tr, dates, period) -> list[str]:
    """Gap, volume, trade prices and six daily bars. Prices are split-adjusted to today, as charts show
    them, unless a later split makes that basis unreadable; then as-traded prices and volumes are shown."""
    p = panel
    j, t = p.index[tr.ticker], p.day[tr.signal_date]
    f = p.factor[:, j]
    as_traded = abs(f[t] - 1) > 1e-9
    px = (lambda i, v: v * f[i]) if as_traded else (lambda i, v: v)
    vol = (lambda i, v: v / f[i]) if as_traded else (lambda i, v: v)
    prev = px(t - 1, p.close_ff[t - 1, j])
    avg = np.nanmean(p.volume[t - 50:t, j] / (f[t - 50:t] if as_traded else 1))
    releases = [d for d in dates.get(tr.ticker, []) if p.dates[t - 1] <= d <= p.dates[t]]
    e = p.day[tr.entry_date]
    note = (f" Prices and volumes are as traded; today's split-adjusted charts show prices x {1 / f[t]:,.6g}."
            if as_traded else "")
    lines = [f"### {tr.ticker}, signal {tr.signal_date} ({PERIOD_TITLES[period]})", "",
             f"Gap {100 * (p.open[t, j] / p.close_ff[t - 1, j] - 1):+.1f}% (prior close {prev:.2f}, open "
             f"{px(t, p.open[t, j]):.2f}); volume {vol(t, p.volume[t, j]):,.0f} = "
             f"{vol(t, p.volume[t, j]) / avg:.1f}x the 50-day average ({avg:,.0f}); earnings date "
             f"{', '.join(releases)}. Close in range "
             f"{100 * (p.close[t, j] - p.low[t, j]) / (p.high[t, j] - p.low[t, j]):.0f}%.{note}", "",
             f"Entry {tr.entry_date} at {px(e, tr.entry_price):.2f}; stop {px(e, tr.stop):.2f} "
             f"({100 * (tr.stop / tr.entry_price - 1):.1f}%); exit {tr.exit_date} at "
             f"{tr.exit_price_as_traded if as_traded else tr.exit_price:.2f} average ({tr.exit_reason}"
             + (f"; a third sold {tr.partial_date} at {px(p.day[tr.partial_date], tr.partial_exit_price):.2f}, "
                f"the rest at {px(p.day[tr.exit_date], tr.final_exit_price):.2f}" if tr.partial_date else "")
             + f"); return {tr.return_pct:+.2f}%, {tr.r:+.2f}R.", "",
             "| Day | Date | Open | High | Low | Close | Volume |", "|---|---|---|---|---|---|---|"]
    for k in range(0, 6):
        i = t + k
        if i >= len(p.dates):
            break
        lines.append(f"| T+{k} | {p.dates[i]} | {px(i, p.open[i, j]):.2f} | {px(i, p.high[i, j]):.2f} | "
                     f"{px(i, p.low[i, j]):.2f} | {px(i, p.close[i, j]):.2f} | {vol(i, p.volume[i, j]):,.0f} |")
    return lines + [""]


if __name__ == "__main__":
    main()
