"""Robustness of the corrected episodic pivot (20% gap, SPY above 50-day, prior-day liquidity, entry at
the gap-day close in the upper half of the range, stop at the gap-day low, exit after 20 sessions),
run as an SPY overlay at 1.0% risk.

Neighbor grid, removal of the best trades, largest winners, a Monte Carlo of out-of-sample trades,
the trades the 1.5% run skipped, and a 20% cash reserve instead of selling SPY.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings  # noqa: E402
import market_filters as mf  # noqa: E402
import report_rules as rr  # noqa: E402
from engine import (OUTPUT, PERIOD_TITLES, PERIODS, Rules, _f, market_series, metrics, run_test,  # noqa: E402
                    simulate, spy_series)
from lab import load_all  # noqa: E402
from strategies import episodic_pivot as ep  # noqa: E402

TWO = ("in_sample", "out_of_sample")
BASE = {**ep.DEFAULTS, "gap_pct": 20, "neglected": False, "liquidity": "prior", "max_split_multiple": 100,
        "split_window_start": "2016-01-01", "market_filter": "B", "exit": {"kind": "time", "days": 20}}
OVERLAY = Rules(overlay_spy=True, risk_pct=1.0)
GAPS, HOLDS = (15, 20, 25), (10, 15, 20, 30)
SIMS, SEED = 5000, 11


def rules_section() -> list[str]:
    return rr.section(
        ["This report tests whether the corrected episodic pivot's result holds up: nearby settings, the removal of "
         "its best trades, resampling, and another way to fund the trades. The strategy under test was frozen in the "
         "corrected report: 20% gap, market filter B, liquidity tested before the signal, exit (b), run as an SPY "
         "overlay at 1.0% risk per trade."],
        rr.EPISODIC_PIVOT,
        ["The frozen version:", "",
         "- Gap threshold 20%; the neglected condition is off.",
         "- Market filter B: new trades only when SPY closed above its 50-day SMA on the signal day.",
         "- " + rr.EP_PRIOR_LIQUIDITY[0],
         "- Exit (b): keep only the initial stop and sell at the close of the 20th session after entry.",
         "- " + rr.SPY_OVERLAY[0] + " Each trade risks 1.0% of equity."],
        ["The checks:", "",
         f"- Sections 1 and 2 run a grid around the frozen version: gaps of {', '.join(f'{g}%' for g in GAPS)}, time "
         f"exits after {', '.join(str(h) for h in HOLDS)} sessions, with and without filter B. Each cell gives "
         "expectancy ± its standard error, the number of trades, and the overlay's CAGR minus SPY's in percentage "
         "points. A result that only works in one cell is likely luck.",
         "- Section 3 removes the 5 and 10 best trades by R from each period's signals and reruns the period; other "
         "signals may take their place.",
         f"- Section 5 resamples the out-of-sample trades {SIMS:,} times with replacement. It estimates each trade's "
         "contribution to the overlay's lead over SPY as its position size times its return minus SPY's over the "
         "same days, less the slippage on selling and rebuying SPY, and compounds three years' worth of trades.",
         "- Section 6 compares the trades taken at 1.0% and 1.5% risk.",
         "- Section 7 funds trades from a cash reserve instead of selling SPY: 80% of equity in SPY, rebalanced at "
         "each month-end close, and 20% in cash that pays for new trades (shrunk or skipped when it runs short) and "
         "receives their proceeds."],
        rr.GROUND_RULES, rr.TERMS)


class Excluding:
    """Drops chosen (session, column) signals; everything else is the wrapped strategy."""

    def __init__(self, inner, excluded: set[tuple[int, int]]):
        self.inner, self.excluded = inner, excluded
        self.name = inner.name

    def setup(self, panel, params):
        self.inner.setup(panel, params)

    def candidates(self, t):
        return np.array([j for j in self.inner.candidates(t) if (t, int(j)) not in self.excluded], dtype=int)

    def __getattr__(self, attr):
        return getattr(self.inner, attr)


def r_stats(trades) -> tuple[float, float, int]:
    rs = np.array([t.r for t in trades])
    return float(rs.mean()), float(rs.std(ddof=1) / math.sqrt(len(rs))), len(rs)


def main() -> None:
    panel, bench = load_all()
    dates = earnings.load(panel)
    masks = mf.compute(panel)["masks"]
    market = market_series(panel, bench)
    shared = {}
    lines = ["# Episodic pivot robustness (SPY overlay, 1.0% risk)", "", f"Data through {panel.dates[-1]}.", "",
             *rules_section()]

    def factory(inner, code):
        return lambda: mf.Filtered(inner, masks[code], code, shared)

    def run_period(strategy, period, rules=OVERLAY):
        start, end = PERIODS[period]
        run = simulate(panel, strategy, start, end, rules, market)
        spy = spy_series(panel, bench["spy_dividends"], start, end or str(panel.dates[-1]))
        return run, metrics(run, spy, bench["tbill3m"])

    # Base (reproduces ep_fixed_b_overlay_1.0).
    base_inner = ep.EpisodicPivot(dates)
    base = run_test(panel, "ep_fixed_b_overlay_1.0", factory(base_inner, "B"), BASE, bench, rules=OVERLAY,
                    description="Episodic pivot, 20% gap, SPY above its 50-day SMA, liquidity tested before the signal day; exit (b) "
                                "initial stop, time exit after 20 sessions; SPY overlay, 1.0% risk.")["results"]
    base_strategy = factory(base_inner, "B")()
    base_strategy.setup(panel, BASE)
    base_runs = {p: run_period(base_strategy, p) for p in TWO}

    # 2. Neighbor grid.
    grid = {}
    for gap in GAPS:
        for hold in HOLDS:
            inner = ep.EpisodicPivot(dates)
            for code in ("B", "A"):
                params = {**BASE, "gap_pct": gap, "exit": {"kind": "time", "days": hold}, "market_filter": code}
                name = f"ep_grid_gap{gap}_hold{hold}_{'spyfilter' if code == 'B' else 'nofilter'}"
                summary = run_test(panel, name, factory(inner, code), params, bench, rules=OVERLAY,
                                   description=f"Episodic pivot, nearby-settings grid: gap {gap}%, time exit after {hold} "
                                               f"sessions, {'SPY above its 50-day SMA' if code == 'B' else 'no market filter'}; "
                                               "liquidity tested before the signal day; SPY overlay, 1.0% risk.")
                grid[(gap, hold, code)] = {}
                for p in (*TWO, "combined"):
                    rs = np.array([float(r["r"]) for r in csv.DictReader(open(OUTPUT / name / f"trades_{p}.csv", encoding="utf-8"))])
                    m = summary["results"][p]
                    grid[(gap, hold, code)][p] = {"expectancy": float(rs.mean()), "se": float(rs.std(ddof=1) / math.sqrt(len(rs))),
                                                  "trades": len(rs), "excess": m["cagr"] - m["spy"]["cagr"], "cagr": m["cagr"]}
                print(f"{name}: done")
    same = all(abs(grid[(20, 20, "B")][p]["cagr"] - base[p]["cagr"]) < 1e-9 for p in TWO)
    lines += ["## 1. Were the gap threshold and the filter chosen on 2016-2021 only?", "",
              f"(The grid's base cell {'reproduces' if same else 'does not reproduce'} the frozen strategy exactly.)", "",
              "In-sample expectancy with a 20-session hold, from the grid below:", "",
              "| Gap | With SPY filter | Without |", "|---|---|---|"]
    for gap in GAPS:
        w, wo = grid[(gap, 20, "B")]["in_sample"], grid[(gap, 20, "A")]["in_sample"]
        lines.append(f"| {gap}% | {w['expectancy']:+.3f}R ± {w['se']:.3f} ({w['trades']}) | "
                     f"{wo['expectancy']:+.3f}R ± {wo['se']:.3f} ({wo['trades']}) |")
    lines.append("")
    for p in TWO:
        lines += [f"## 2. Neighbor grid, {PERIOD_TITLES[p]}", "",
                  "Each cell: expectancy ± standard error (trades); overlay CAGR minus SPY.", "",
                  "| Gap | Filter | " + " | ".join(f"Hold {h}" for h in HOLDS) + " |", "|---|---|" + "---|" * len(HOLDS)]
        for gap in GAPS:
            for code in ("B", "A"):
                cells = [f"{grid[(gap, h, code)][p]['expectancy']:+.3f}R ± {grid[(gap, h, code)][p]['se']:.3f} "
                         f"({grid[(gap, h, code)][p]['trades']}); {grid[(gap, h, code)][p]['excess']:+.2f}" for h in HOLDS]
                lines.append(f"| {gap}% | {'SPY above 50-day' if code == 'B' else 'none'} | " + " | ".join(cells) + " |")
        lines.append("")

    # 3 and 4. Best trades.
    lines += ["## 3. Without the most profitable trades (ranked by R, removed from their own period)", "",
              "| Period | Version | Trades | Expectancy ± SE | CAGR | CAGR minus SPY | Max drawdown | Sharpe |",
              "|---|---|---|---|---|---|---|---|"]
    winners = {}
    for p in TWO:
        run, m = base_runs[p]
        ranked = sorted(run["trades"], key=lambda t: -t.r)
        winners[p] = ranked
        for k in (0, 5, 10):
            excluded = {(panel.day[t.signal_date], panel.index[t.ticker]) for t in ranked[:k]}
            strat = Excluding(base_strategy, excluded)
            r2, m2 = run_period(strat, p)
            e, se, n = r_stats(r2["trades"])
            lines.append(f"| {PERIOD_TITLES[p]} | {'all trades' if k == 0 else f'without the top {k}'} | {n} | "
                         f"{e:+.3f}R ± {se:.3f} | {m2['cagr']:+.2f}% | {m2['cagr'] - m2['spy']['cagr']:+.2f} | "
                         f"{m2['max_drawdown']:.1f}% | {m2['sharpe']:.2f} |")
    lines += ["", "## 4. Ten largest winners by R", ""]
    for p in TWO:
        ranked = winners[p]
        total = sum(t.r for t in ranked)
        top = ranked[:10]
        lines += [f"### {PERIOD_TITLES[p]}: total {total:+.1f}R over {len(ranked)} trades; the top 10 made "
                  f"{sum(t.r for t in top):+.1f}R = {100 * sum(t.r for t in top) / total:.0f}% of it", "",
                  "| Ticker | Signal date | Exit | R | Return |", "|---|---|---|---|---|"]
        for t in top:
            lines.append(f"| {t.ticker} | {t.signal_date} | {t.exit_date} ({t.exit_reason}) | {t.r:+.2f}R | {t.return_pct:+.1f}% |")
        lines.append("")

    # 5. Monte Carlo of out-of-sample trades.
    run, m = base_runs["out_of_sample"]
    curve = dict(zip(m["equity_curve"]["dates"], m["equity_curve"]["equity"]))
    days = m["equity_curve"]["dates"]
    tr_idx = market["spy_tr"]
    rows = []
    for t in run["trades"]:
        i = days.index(t.entry_date)
        before = curve[days[i - 1]] if i > 0 else OVERLAY.capital
        f = t.position_value / before
        spy = tr_idx[panel.day[t.exit_date]] / tr_idx[panel.day[t.entry_date]] - 1
        rows.append((t.r, f * (t.return_pct / 100 - spy) - f * 2 * OVERLAY.slippage_pct / 100))
    rs, contrib = np.array([r for r, _ in rows]), np.array([c for _, c in rows])
    years = (np.datetime64(days[-1]) - np.datetime64(days[0])).astype(int) / 365.25
    actual_relative = (1 + m["total_return"] / 100) / (1 + m["spy"]["total_return"] / 100) - 1
    modelled_relative = float(np.prod(1 + contrib) - 1)
    n3 = int(round(len(rs) * 3 / years))
    rng = np.random.default_rng(SEED)
    exp_draws = rs[rng.integers(0, len(rs), size=(SIMS, len(rs)))].mean(axis=1)
    rel_draws = np.prod(1 + contrib[rng.integers(0, len(rs), size=(SIMS, n3))], axis=1) - 1
    pct = lambda a: np.percentile(a, [5, 50, 95])  # noqa: E731
    e5, e50, e95 = pct(exp_draws)
    x5, x50, x95 = pct(rel_draws)
    lines += ["## 5. Monte Carlo of out-of-sample trades", "",
              f"{SIMS:,} resamples with replacement of the {len(rs)} out-of-sample trades. Each trade's contribution "
              "to the overlay's excess over SPY is its position size (share of equity) x (its return minus SPY's return "
              "over the same days), minus the slippage on selling and rebuying SPY. A 3-year path draws "
              f"{n3} trades (the out-of-sample rate of {len(rs) / years:.1f} a year) and compounds them.", "",
              f"Model check on the actual out-of-sample sequence: modelled relative excess {100 * modelled_relative:+.1f}% "
              f"vs actual {100 * actual_relative:+.1f}% (overlay growth / SPY growth - 1, {years:.1f} years).", "",
              "| | 5th percentile | Median | 95th percentile | Share below zero |", "|---|---|---|---|---|",
              f"| Expectancy | {e5:+.3f}R | {e50:+.3f}R | {e95:+.3f}R | {100 * (exp_draws <= 0).mean():.1f}% |",
              f"| 3-year overlay excess over SPY (cumulative) | {100 * x5:+.1f}% | {100 * x50:+.1f}% | {100 * x95:+.1f}% | "
              f"{100 * (rel_draws <= 0).mean():.1f}% |",
              f"| Same, per year | {100 * ((1 + x5) ** (1 / 3) - 1):+.2f}% | {100 * ((1 + x50) ** (1 / 3) - 1):+.2f}% | "
              f"{100 * ((1 + x95) ** (1 / 3) - 1):+.2f}% | |", ""]

    # 6. Trades the 1.5% run skipped.
    lines += ["## 6. Why the 1.5% run took fewer trades than the 1.0% run", ""]
    skipped_info = {}
    for p in TWO:
        r10, _ = run_period(base_strategy, p, Rules(overlay_spy=True, risk_pct=1.0))
        r15, _ = run_period(base_strategy, p, Rules(overlay_spy=True, risk_pct=1.5))
        k10 = {(t.ticker, t.signal_date) for t in r10["trades"]}
        k15 = {(t.ticker, t.signal_date) for t in r15["trades"]}
        missing, extra = sorted(k10 - k15, key=lambda k: k[1]), sorted(k15 - k10, key=lambda k: k[1])
        skips15 = {(s["ticker"], s["date"]): s for s in r15["skips"]}
        shrunk15 = [s for s in r15["skips"] if s["reason"] == "shrunk to the cash available"]
        skipped_info[p] = {"missing": missing, "extra": extra}
        lines += [f"### {PERIOD_TITLES[p]}: {len(r10['trades'])} trades at 1.0%, {len(r15['trades'])} at 1.5%", ""]
        if missing:
            lines += ["| Ticker | Signal date | Constraint at 1.5% | Open positions | Equity already in positions | "
                      "Wanted | Cash available |", "|---|---|---|---|---|---|---|"]
            for key in missing:
                s = skips15.get(key, {"reason": "not signalled (see note)"})
                lines.append(f"| {key[0]} | {key[1]} | {s['reason']} | {s.get('open_positions', '')} | "
                             f"{_f(s.get('invested_pct'), 0, '%') if 'invested_pct' in s else ''} | "
                             f"{_f(s.get('wanted_pct'), 1, '%') if 'wanted_pct' in s else ''} | "
                             f"{_f(s.get('available_pct'), 1, '%') if 'available_pct' in s else ''} |")
            lines.append("")
        if extra:
            lines += [f"Taken at 1.5% but not at 1.0%: {', '.join(f'{a} {b}' for a, b in extra)}.", ""]
        if shrunk15:
            lines += [f"Entries shrunk to the cash available at 1.5%: " + ", ".join(
                f"{s['ticker']} {s['date']} ({s['wanted_pct']:.0f}% wanted, {s['available_pct']:.0f}% available)"
                for s in shrunk15) + ".", ""]

    # 7. 20% cash reserve instead of selling SPY.
    reserve = {}
    for label, rules in (("sell SPY to fund trades", OVERLAY),
                         ("20% cash reserve, reserve earns nothing", Rules(overlay_spy=True, risk_pct=1.0, reserve_pct=20)),
                         ("20% cash reserve, reserve earns T-bills",
                          Rules(overlay_spy=True, risk_pct=1.0, reserve_pct=20, idle_cash_tbill=True))):
        name = {"sell SPY to fund trades": "ep_fixed_b_overlay_1.0",
                "20% cash reserve, reserve earns nothing": "ep_fixed_b_reserve20_1.0",
                "20% cash reserve, reserve earns T-bills": "ep_fixed_b_reserve20_1.0_tbill"}[label]
        reserve[label] = run_test(panel, name, factory(base_inner, "B"), BASE, bench, rules=rules,
                                  description=("Episodic pivot, 20% gap, SPY above its 50-day SMA, liquidity tested before the "
                                               "signal day; exit (b) initial stop, time exit after 20 sessions; SPY "
                                               f"overlay at 1.0% risk; funding: {label}."))["results"]
    lines += ["## 7. Funding from a 20% cash reserve instead of selling SPY", "",
              "Reserve mode: 80% of equity in SPY, rebalanced at each month-end close; new trades are paid for from the "
              "cash reserve only (shrunk or skipped if it runs short) and their proceeds return to it.", "",
              "| Period | Funding | Trades | CAGR | CAGR minus SPY | Max drawdown | Sharpe | Shrunk / skipped for cash |",
              "|---|---|---|---|---|---|---|---|"]
    for p in TWO:
        for label, res in reserve.items():
            m = res[p]
            lines.append(f"| {PERIOD_TITLES[p]} | {label} | {m['trades']} | {m['cagr']:+.2f}% | "
                         f"{m['cagr'] - m['spy']['cagr']:+.2f} | {m['max_drawdown']:.1f}% | {m['sharpe']:.2f} | "
                         f"{m['counts'].get('cash_limited_entries', 0)} / {m['counts'].get('skipped_no_cash', 0)} |")
        spy = reserve["sell SPY to fund trades"][p]["spy"]
        lines.append(f"| {PERIOD_TITLES[p]} | SPY buy-and-hold | | {spy['cagr']:+.2f}% | | {spy['max_drawdown']:.1f}% | {spy['sharpe']:.2f} | |")
    lines.append("")
    path = OUTPUT / "ep_robustness_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUT / "ep_robustness_report.json").write_text(json.dumps(
        {"grid": {f"{g}|{h}|{c}": v for (g, h, c), v in grid.items()},
         "monte_carlo": {"expectancy": [e5, e50, e95], "three_year_excess": [x5, x50, x95], "n3": n3,
                         "modelled_relative": modelled_relative, "actual_relative": actual_relative},
         "skipped": skipped_info}, indent=1, default=float), encoding="utf-8")
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
