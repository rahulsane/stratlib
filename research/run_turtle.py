"""The Turtle Trading System on SPY, QQQ, GLD, SLV, USO and TLT, compared with SPY and QQQ buy-and-hold.

Variants: System 1, System 2, and half the capital in each (two accounts, summed), each with futures-style
leverage as the rules size it, with gross exposure capped at 2x equity (Reg T margin), and capped at 1x (no
borrowing). Periods: the full common history from August 2006, 2006-2015, and the harness's in-sample,
out-of-sample and combined windows. The Turtle parameters date from 1983 and none were chosen here, so no
period is used to pick anything.

Run: PYTHONPATH="src;research" .venv/Scripts/python research/run_turtle.py
Output: research/output/turtle/ (report.md, results.json, curves_full.csv, trades/)
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np

import turtle_data as td
import turtle_sim as ts

OUT = Path(__file__).resolve().parent / "output" / "turtle"
CAPITAL = 100_000.0

PERIODS = {
    "full": ("Full period", "2006-08-01", "9999"),
    "early": ("2006–2015", "2006-08-01", "2015-12-31"),
    "is": ("In-sample (2016–2021)", "2016-01-04", "2021-12-31"),
    "oos": ("Out-of-sample (2022–present)", "2022-01-03", "9999"),
    "combined": ("Combined (2016–present)", "2016-01-04", "9999"),
}
SYSTEMS = {"S1": ((1, 1.0),), "S2": ((2, 1.0),), "S1+S2": ((1, 0.5), (2, 0.5))}
SYSTEM_NAMES = {"S1": "System 1 (20/10)", "S2": "System 2 (55/20)", "S1+S2": "Half in each"}
LEVERAGE = {"as written": None, "2x cap": 2.0, "1x cap": 1.0}
VARIANTS = [(s, lev) for lev in LEVERAGE for s in SYSTEMS]
SENSITIVITY = [
    ("As tested", {}),
    ("Slippage 0.05% per side, no low-price tier", {"slippage": 0.0005, "slippage_low": 0.0005}),
    ("No slippage", {"slippage": 0.0, "slippage_low": 0.0}),
    ("Cash earns nothing", {"cash_interest": False}),
    ("No drawdown rule", {"drawdown_rule": False}),
    ("System 1 without the last-breakout filter", {"s1_filter": False}),
    ("Long only", {"long_only": True}),
    ("Half size: 0.5% per N", {"risk_per_n": 0.005}),
]


def key(system: str, lev: str) -> str:
    return f"{system} {lev}"


def window(data, period: str) -> tuple[int, int]:
    _, a, b = PERIODS[period]
    t0 = int(np.searchsorted(data.days, a))
    t1 = int(np.searchsorted(data.days, b, side="right")) - 1
    return t0, t1


def run_variant(data, ind, system: str, lev: str, period: str, **overrides) -> dict:
    t0, t1 = window(data, period)
    runs = []
    for sysno, share in SYSTEMS[system]:
        cfg = ts.Config(system=sysno, capital=CAPITAL * share, max_gross=LEVERAGE[lev], **overrides)
        runs.append(ts.run(cfg, data, ind, t0, t1))
    out = ts.combine(runs) if len(runs) > 1 else runs[0]
    out["t0"], out["t1"] = t0, t1
    return out


# ----- statistics -----

def _years(d0: str, d1: str) -> float:
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25


def curve(run: dict, data) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t0, t1 = run["t0"], run["t1"]
    eq = np.concatenate([[run["start_equity"]], run["equity"]])
    return data.days[t0:t1 + 1], eq, data.rate[t0:t1 + 1]


def bench_curve(data, symbol: str, t0: int, t1: int) -> np.ndarray:
    j = data.symbols.index(symbol)
    return CAPITAL * data.close[t0:t1 + 1, j] / data.close[t0, j]


def yearly(dates: np.ndarray, eq: np.ndarray) -> dict[str, float]:
    out, prev = {}, eq[0]
    for i in range(1, len(dates)):
        if i == len(dates) - 1 or dates[i][:4] != dates[i + 1][:4]:
            out[dates[i][:4]] = 100 * (eq[i] / prev - 1)
            prev = eq[i]
    return out


def drawdowns(eq: np.ndarray) -> np.ndarray:
    return eq / np.maximum.accumulate(eq) - 1


def curve_stats(dates, eq, rate, spy, qqq) -> dict:
    ruined = bool((eq <= 0).any())
    eq = np.maximum(eq, 0.01)          # a wiped-out account counts as a 100% loss
    r, rs, rq = eq[1:] / eq[:-1] - 1, spy[1:] / spy[:-1] - 1, qqq[1:] / qqq[:-1] - 1
    rf = rate[1:] / 100 / 252
    years = _years(dates[0], dates[-1])
    cagr = lambda x: 100 * ((x[-1] / x[0]) ** (1 / years) - 1)
    ex = r - rf
    sd = ex.std(ddof=1)
    downside = np.sqrt(np.mean(np.minimum(ex, 0) ** 2))
    dd = drawdowns(eq)
    longest = run_ = longest_to = 0
    for i, x in enumerate(dd):
        run_ = run_ + 1 if x < 0 else 0
        if run_ > longest:
            longest, longest_to = run_, i
    longest_from = longest_to - longest
    recovered = longest_to + 1 < len(dates)
    me = [i for i in range(len(dates) - 1) if dates[i][:7] != dates[i + 1][:7]] + [len(dates) - 1]
    mr = eq[me][1:] / eq[me][:-1] - 1
    ms = spy[me][1:] / spy[me][:-1] - 1
    roll, roll_s, roll_q = (x[252:] / x[:-252] - 1 for x in (eq, spy, qqq))
    ys, yspy, yqqq = yearly(dates, eq), yearly(dates, spy), yearly(dates, qqq)
    te = 100 * (r - rs).std(ddof=1) * np.sqrt(252)
    c, cs, cq = cagr(eq), cagr(spy), cagr(qqq)
    max_dd = -100 * dd.min()
    return {
        "total_return": 100 * (eq[-1] / eq[0] - 1), "final": float(eq[-1]), "cagr": c, "cagr_spy": cs, "cagr_qqq": cq,
        "vol": 100 * r.std(ddof=1) * np.sqrt(252),
        "sharpe": float(ex.mean() / sd * np.sqrt(252)) if sd > 0 else np.nan,
        "sortino": float(ex.mean() / downside * np.sqrt(252)) if downside > 0 else np.nan,
        "max_dd": max_dd, "calmar": c / max_dd if max_dd > 0 else np.nan,
        "longest_dd_months": longest / 21, "longest_dd_from": str(dates[longest_from]),
        "longest_dd_to": str(dates[longest_to + 1]) if recovered else "not yet recovered",
        "worst_day": 100 * r.min(), "worst_day_date": str(dates[1:][r.argmin()]),
        "worst_month": 100 * mr.min(), "worst_12m": 100 * roll.min() if len(roll) else np.nan,
        "best_year": max(ys.values()), "worst_year": min(ys.values()),
        "positive_years": sum(v > 0 for v in ys.values()), "n_years": len(ys),
        "beta": float(np.cov(r, rs)[0, 1] / rs.var(ddof=1)), "corr_spy": float(np.corrcoef(r, rs)[0, 1]),
        "corr_qqq": float(np.corrcoef(r, rq)[0, 1]), "corr_spy_monthly": float(np.corrcoef(mr, ms)[0, 1]),
        "tracking_error": te, "info_ratio": (c - cs) / te if te > 0 else np.nan,
        "years_beat_spy": sum(ys[y] > yspy[y] for y in ys), "years_beat_qqq": sum(ys[y] > yqqq[y] for y in ys),
        "roll12_beat_spy": 100 * float((roll > roll_s).mean()) if len(roll) else np.nan,
        "roll12_beat_qqq": 100 * float((roll > roll_q).mean()) if len(roll) else np.nan,
        "months_beat_spy": 100 * float((mr > ms).mean()),
        "yearly": ys, "years": years, "ruined": ruined,
    }


def bench_stats(dates, eq, rate, spy, qqq) -> dict:
    return curve_stats(dates, eq, rate, spy, qqq)


def exposure_stats(run: dict) -> dict:
    eq = run["equity"]
    g, n = run["gross"] / eq, run["net"] / eq
    return {"time_in": 100 * float((run["positions"] > 0).mean()), "avg_gross": float(g.mean()),
            "max_gross": float(g.max()), "avg_net": float(n.mean()),
            "avg_units_long": float(run["units_long"].mean()), "avg_units_short": float(run["units_short"].mean()),
            "max_units": int((run["units_long"] + run["units_short"]).max()),
            "cut_days": 100 * float((run["cuts"] > 0).mean()),
            "deep_cut_days": 100 * float((run["cuts"] >= 3).mean())}


def trade_stats(trades: list[dict], years: float) -> dict:
    if not trades:
        return {"trades": 0}
    pnl = np.array([t["pnl"] for t in trades])
    rs = np.array([t["r"] for t in trades])
    pct = np.array([t["pct"] for t in trades])
    hold = np.array([t["holding_days"] for t in trades])
    win = pnl > 0
    order = np.sort(rs)[::-1]
    total = pnl.sum()
    top = np.sort(pnl)[::-1][:max(1, len(pnl) // 10)].sum()
    streak = run_ = 0
    for w in win:
        run_ = 0 if w else run_ + 1
        streak = max(streak, run_)
    exits: dict[str, int] = {}
    for t in trades:
        for e in t["unit_exits"]:
            exits[e] = exits.get(e, 0) + 1
    entries: dict[str, int] = {}
    for t in trades:
        entries[t["entry"]] = entries.get(t["entry"], 0) + 1
    by_market: dict[str, float] = {}
    for t in trades:
        by_market[t["symbol"]] = by_market.get(t["symbol"], 0.0) + t["pnl"]
    return {
        "trades": len(trades), "per_year": len(trades) / years, "win_rate": 100 * float(win.mean()),
        "avg_win_pct": float(pct[win].mean()) if win.any() else np.nan,
        "avg_win_r": float(rs[win].mean()) if win.any() else np.nan,
        "avg_loss_pct": float(pct[~win].mean()) if (~win).any() else np.nan,
        "avg_loss_r": float(rs[~win].mean()) if (~win).any() else np.nan,
        "expectancy": float(rs.mean()), "se": float(rs.std(ddof=1) / np.sqrt(len(rs))) if len(rs) > 1 else np.nan,
        "exp_wo_top5": float(order[5:].mean()) if len(order) > 5 else np.nan,
        "profit_factor": float(pnl[win].sum() / -pnl[~win].sum()) if (~win).any() else np.nan,
        "hold_win": float(hold[win].mean()) if win.any() else np.nan,
        "hold_loss": float(hold[~win].mean()) if (~win).any() else np.nan,
        "top10_share": 100 * top / total if total > 0 else np.nan, "top10_dollars": float(top),
        "rest_dollars": float(total - top), "total_pnl": float(total),
        "best_share": 100 * pnl.max() / total if total > 0 else np.nan,
        "losing_streak": streak, "avg_units": float(np.mean([t["units"] for t in trades])),
        "long_pnl": float(sum(t["pnl"] for t in trades if t["side"] == "long")),
        "short_pnl": float(sum(t["pnl"] for t in trades if t["side"] == "short")),
        "long_trades": sum(t["side"] == "long" for t in trades),
        "by_market": by_market, "exits": exits, "entries": entries,
    }


def summarize(run: dict, data, bench_data=None) -> dict:
    """bench_data supplies SPY and QQQ when the run's own markets leave one out (same sessions required)."""
    bench_data = bench_data if bench_data is not None else data
    dates, eq, rate = curve(run, data)
    spy = bench_curve(bench_data, "SPY", run["t0"], run["t1"])
    qqq = bench_curve(bench_data, "QQQ", run["t0"], run["t1"])
    c = curve_stats(dates, eq, rate, spy, qqq)
    avg_eq = float(np.mean(eq))
    flows = {k: 100 * v / avg_eq / c["years"] for k, v in run["flows"].items()}
    return {"curve": c, "exposure": exposure_stats(run), "trades": trade_stats(run["trades"], c["years"]),
            "flows_pct_per_year": flows, "flows": run["flows"], "blocked": run["blocked"], "ruined": run["ruined"],
            "start": str(dates[0]), "end": str(dates[-1])}


def scaled(dates, eq, rate, target_vol: float) -> dict:
    """The same daily returns with the excess over T-bills scaled to a target volatility."""
    r, rf = eq[1:] / eq[:-1] - 1, rate[1:] / 100 / 252
    k = target_vol / (100 * r.std(ddof=1) * np.sqrt(252))
    rk = rf + k * (r - rf)
    curve_ = CAPITAL * np.concatenate([[1.0], np.cumprod(1 + rk)])
    years = _years(dates[0], dates[-1])
    ex = rk - rf
    return {"k": k, "cagr": 100 * ((curve_[-1] / CAPITAL) ** (1 / years) - 1), "max_dd": -100 * drawdowns(curve_).min(),
            "sharpe": float(ex.mean() / ex.std(ddof=1) * np.sqrt(252))}


def spy_declines(spy: np.ndarray, dates: np.ndarray, depth: float = 0.15) -> list[tuple[int, int]]:
    """Peak-to-trough SPY declines of at least `depth`, each ending at its lowest point before a new high."""
    out, peak, trough = [], 0, 0
    for i in range(1, len(spy)):
        if spy[i] >= spy[peak]:
            if spy[trough] / spy[peak] - 1 <= -depth:
                out.append((peak, trough))
            peak = trough = i
        elif spy[i] < spy[trough]:
            trough = i
    if spy[trough] / spy[peak] - 1 <= -depth:
        out.append((peak, trough))
    return out


def write_trades(path: Path, trades: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["symbol", "side", "entry", "entry_date", "exit_date", "holding_days", "sessions", "units", "avg_entry",
            "avg_exit", "position_value", "pnl", "r", "pct", "exit_reason", "unit_exits", "cost"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in trades:
            w.writerow([";".join(t[c]) if c == "unit_exits" else
                        (round(t[c], 4) if isinstance(t[c], float) else t[c]) for c in cols])


# ----- report -----

def f(x, digits=1, suffix="", sign=False) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:+.{digits}f}{suffix}" if sign else f"{x:,.{digits}f}{suffix}"


def table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


CURVE_ROWS = [
    ("Ending value of $100,000", lambda c: f"${c['final']:,.0f}"),
    ("CAGR", lambda c: f(c["cagr"], 1, "%")),
    ("CAGR minus SPY / minus QQQ", lambda c: f"{f(c['cagr'] - c['cagr_spy'], 1, '', True)} / {f(c['cagr'] - c['cagr_qqq'], 1, '', True)} pts"),
    ("Annualized volatility", lambda c: f(c["vol"], 1, "%")),
    ("Sharpe (over T-bills)", lambda c: f(c["sharpe"], 2)),
    ("Sortino", lambda c: f(c["sortino"], 2)),
    ("Max drawdown", lambda c: f(c["max_dd"], 1, "%")),
    ("Calmar: CAGR / max drawdown", lambda c: f(c["calmar"], 2)),
    ("Longest time under a previous high", lambda c: f"{c['longest_dd_months']:.0f} months"),
    ("Worst day", lambda c: f"{f(c['worst_day'], 1, '%')} ({c['worst_day_date']})"),
    ("Worst month", lambda c: f(c["worst_month"], 1, "%")),
    ("Worst rolling 12 months", lambda c: f(c["worst_12m"], 1, "%")),
    ("Best / worst calendar year", lambda c: f"{f(c['best_year'], 1, '%')} / {f(c['worst_year'], 1, '%')}"),
    ("Positive calendar years", lambda c: f"{c['positive_years']} of {c['n_years']}"),
    ("Beta / correlation to SPY (daily)", lambda c: f"{c['beta']:.2f} / {c['corr_spy']:.2f}"),
    ("Correlation to SPY (monthly)", lambda c: f"{c['corr_spy_monthly']:.2f}"),
    ("Correlation to QQQ", lambda c: f"{c['corr_qqq']:.2f}"),
    ("Tracking error / information ratio vs SPY", lambda c: f"{f(c['tracking_error'], 1, '%')} / {f(c['info_ratio'], 2)}"),
    ("Calendar years beating SPY / QQQ", lambda c: f"{c['years_beat_spy']} / {c['years_beat_qqq']} of {c['n_years']}"),
    ("Rolling 12-month windows beating SPY / QQQ", lambda c: f"{f(c['roll12_beat_spy'], 0, '%')} / {f(c['roll12_beat_qqq'], 0, '%')}"),
    ("Months beating SPY", lambda c: f(c["months_beat_spy"], 0, "%")),
]
EXPOSURE_ROWS = [
    ("Time with a position", lambda e: f(e["time_in"], 0, "%")),
    ("Gross exposure: average / most (x equity)", lambda e: f"{e['avg_gross']:.2f}x / {e['max_gross']:.2f}x"),
    ("Net exposure: average (x equity)", lambda e: f"{e['avg_net']:+.2f}x"),
    ("Units held: average long / short; most", lambda e: f"{e['avg_units_long']:.1f} / {e['avg_units_short']:.1f}; {e['max_units']}"),
    ("Sessions trading a reduced notional: any cut / 3+ cuts (notional 51% or less)",
     lambda e: f"{f(e['cut_days'], 0, '%')} / {f(e['deep_cut_days'], 0, '%')}"),
]
TRADE_ROWS = [
    ("Trades (positions) / per year", lambda t: f"{t['trades']:,} / {t['per_year']:.0f}"),
    ("Long / short trades", lambda t: f"{t['long_trades']} / {t['trades'] - t['long_trades']}"),
    ("Win rate", lambda t: f(t["win_rate"], 1, "%")),
    ("Average win", lambda t: f"{f(t['avg_win_pct'], 2, '%')} / {f(t['avg_win_r'], 2, 'R')}"),
    ("Average loss", lambda t: f"{f(t['avg_loss_pct'], 2, '%')} / {f(t['avg_loss_r'], 2, 'R')}"),
    ("Expectancy ± SE", lambda t: f"{f(t['expectancy'], 3, 'R', True)} ± {f(t['se'], 3)}"),
    ("Expectancy without the top 5 trades", lambda t: f(t["exp_wo_top5"], 3, "R", True)),
    ("Profit factor", lambda t: f(t["profit_factor"], 2)),
    ("Average holding: winners / losers", lambda t: f"{t['hold_win']:.0f} / {t['hold_loss']:.0f} days"),
    ("Units per position (average)", lambda t: f(t["avg_units"], 2)),
    ("Longest run of losing trades", lambda t: f"{t['losing_streak']}"),
    ("Top 10% of trades, share of profit", lambda t: (f(t["top10_share"], 0, "%") if t["total_pnl"] > 0 else
                                                      f"net loss: top made ${t['top10_dollars']:,.0f}, the rest ${t['rest_dollars']:,.0f}")),
    ("Best single trade, share of profit", lambda t: f(t["best_share"], 0, "%")),
    ("Trade P&L: long / short", lambda t: f"${t['long_pnl']:,.0f} / ${t['short_pnl']:,.0f}"),
]
FLOW_ROWS = [
    ("T-bill interest on cash", "interest"), ("Margin interest paid", "financing"),
    ("Short borrow fees", "borrow"), ("Slippage", "slippage"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = td.load()
    ind = ts.indicators(data)
    results: dict = {"data_through": str(data.days[-1]), "symbols": list(data.symbols), "runs": {}}
    runs: dict = {}
    for period in PERIODS:
        for system, lev in VARIANTS:
            run = run_variant(data, ind, system, lev, period)
            runs[(period, system, lev)] = run
            results["runs"][f"{period} | {key(system, lev)}"] = summarize(run, data)
            write_trades(OUT / "trades" / f"{period}_{system.replace('+', '_')}_{lev.replace(' ', '_')}.csv",
                         run["trades"])
    bench = {}
    for period in PERIODS:
        t0, t1 = window(data, period)
        dates, rate = data.days[t0:t1 + 1], data.rate[t0:t1 + 1]
        spy, qqq = bench_curve(data, "SPY", t0, t1), bench_curve(data, "QQQ", t0, t1)
        bench[period] = {"SPY": curve_stats(dates, spy, rate, spy, qqq), "QQQ": curve_stats(dates, qqq, rate, spy, qqq),
                         "dates": dates, "spy": spy, "qqq": qqq, "rate": rate}
        results["benchmarks_" + period] = {k: bench[period][k] for k in ("SPY", "QQQ")}

    # sensitivity, full period
    sens = {}
    for label, over in SENSITIVITY:
        for system, lev in (("S1", "as written"), ("S2", "as written"), ("S1+S2", "as written"), ("S1+S2", "1x cap")):
            run = run_variant(data, ind, system, lev, "full", **over)
            sens[(label, system, lev)] = summarize(run, data)["curve"] | {"ruined_on": run["ruined"]}
    results["sensitivity"] = {f"{a} | {b} {c}": v for (a, b, c), v in sens.items()}

    # vol-matched comparison, full period
    b = bench["full"]
    matched = {}
    for system, lev in VARIANTS:
        if lev != "as written":
            continue
        dates, eq, rate = curve(runs[("full", system, lev)], data)
        matched[system] = {"spy": scaled(dates, eq, rate, b["SPY"]["vol"]), "qqq": scaled(dates, eq, rate, b["QQQ"]["vol"])}
    results["vol_matched"] = matched

    # SPY declines of 15% or more, full period
    declines = []
    for p, q in spy_declines(b["spy"], b["dates"]):
        row = {"peak": str(b["dates"][p]), "trough": str(b["dates"][q]), "SPY": 100 * (b["spy"][q] / b["spy"][p] - 1),
               "QQQ": 100 * (b["qqq"][q] / b["qqq"][p] - 1)}
        for system, lev in VARIANTS:
            _, eq, _ = curve(runs[("full", system, lev)], data)
            row[key(system, lev)] = 100 * (eq[q] / eq[p] - 1)
        declines.append(row)
    results["spy_declines"] = declines

    # CAGR from the full period's start through earlier year-ends
    through = {}
    for end in ("2019-12-31", "2024-12-31", "2025-12-31"):
        i = int(np.searchsorted(b["dates"], end, side="right")) - 1
        yrs = _years(b["dates"][0], b["dates"][i])
        row = {n: 100 * ((b[n.lower()][i] / CAPITAL) ** (1 / yrs) - 1) for n in ("SPY", "QQQ")}
        for system, lev in VARIANTS:
            _, eq, _ = curve(runs[("full", system, lev)], data)
            row[key(system, lev)] = 100 * ((eq[i] / CAPITAL) ** (1 / yrs) - 1)
        through[end] = row
    results["cagr_through"] = through

    # curves for charts
    with (OUT / "curves_full.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "SPY", "QQQ"] + [key(s, lev) for s, lev in VARIANTS])
        cols = [curve(runs[("full", s, lev)], data)[1] for s, lev in VARIANTS]
        for i, d in enumerate(b["dates"]):
            w.writerow([d, round(b["spy"][i], 2), round(b["qqq"][i], 2)] + [round(c[i], 2) for c in cols])

    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    (OUT / "report.md").write_text(report(results, data, bench), encoding="utf-8")
    print((OUT / "report.md").read_text(encoding="utf-8")[:6000])


def report(res: dict, data, bench: dict) -> str:
    R = res["runs"]
    L = []
    L.append("# The Turtle Trading System on six ETFs")
    L.append("")
    L.append(f"Data through {res['data_through']}. Rules: *The Original Turtle Trading Rules* (2003). Markets: SPY, QQQ, GLD (gold), SLV (silver), USO (crude oil) and TLT (20+ year "
             "Treasuries, standing in for the 30-year bond). Benchmarks: SPY and QQQ bought at the first close and held, "
             "with dividends.")
    L.append("")
    L.append(findings(res))
    L.append("")
    L.append("![Growth of $100,000 and drawdowns](equity_drawdown.png)")
    L.append("")
    # summary across variants and periods
    L.append("## Summary: CAGR / max drawdown / Sharpe")
    L.append("")
    hdr = ["Variant"] + [PERIODS[p][0] for p in PERIODS]
    rows = []
    for system, lev in VARIANTS:
        cells = []
        for p in PERIODS:
            c = R[f"{p} | {key(system, lev)}"]["curve"]
            cells.append(f"{f(c['cagr'], 1, '%')} / {f(c['max_dd'], 0, '%')} / {f(c['sharpe'], 2)}")
        rows.append([f"{SYSTEM_NAMES[system]}, {lev}"] + cells)
    for name in ("SPY", "QQQ"):
        cells = []
        for p in PERIODS:
            c = res["benchmarks_" + p][name]
            cells.append(f"{f(c['cagr'], 1, '%')} / {f(c['max_dd'], 0, '%')} / {f(c['sharpe'], 2)}")
        rows.append([f"{name} buy-and-hold"] + cells)
    L.append(table(hdr, rows))
    L.append("")
    for p in PERIODS:
        a, b_ = R[f"{p} | {key('S1', 'as written')}"]["start"], R[f"{p} | {key('S1', 'as written')}"]["end"]
        L.append(f"- {PERIODS[p][0]}: {a} close to {b_} close.")
    L.append("")

    for lev_group, title in ((["as written"], "Rules as written (futures-style leverage)"),
                             (["2x cap", "1x cap"], "Gross exposure capped at 2x and 1x equity")):
        for p in ("full", "combined"):
            L.append(f"## {title}: {PERIODS[p][0].lower()}")
            L.append("")
            cols = [(s, lev) for lev in lev_group for s in SYSTEMS]
            hdr = [""] + [f"{SYSTEM_NAMES[s]}{'' if lev == 'as written' else ', ' + lev}" for s, lev in cols] + ["SPY", "QQQ"]
            rows = []
            for label, fn in CURVE_ROWS:
                rows.append([label] + [fn(R[f"{p} | {key(s, lev)}"]["curve"]) for s, lev in cols] +
                            [fn(res["benchmarks_" + p][n]) for n in ("SPY", "QQQ")])
            for label, fn in EXPOSURE_ROWS:
                rows.append([label] + [fn(R[f"{p} | {key(s, lev)}"]["exposure"]) for s, lev in cols] + ["", ""])
            for label, fn in TRADE_ROWS:
                rows.append([label] + [fn(R[f"{p} | {key(s, lev)}"]["trades"]) for s, lev in cols] + ["", ""])
            for label, k in FLOW_ROWS:
                rows.append([label + " (% of average equity a year)"] +
                            [f(R[f"{p} | {key(s, lev)}"]["flows_pct_per_year"][k], 2, "%", True) for s, lev in cols] + ["", ""])
            L.append(table(hdr, rows))
            L.append("")

    # yearly
    L.append("## Calendar years (full-period runs)")
    L.append("")
    cols = VARIANTS
    hdr = ["Year"] + [f"{s}, {lev}" for s, lev in cols] + ["SPY", "QQQ"]
    years = list(R[f"full | {key('S1', 'as written')}"]["curve"]["yearly"])
    rows = []
    for y in years:
        rows.append([y] + [f(R[f"full | {key(s, lev)}"]["curve"]["yearly"][y], 1, "%") for s, lev in cols] +
                    [f(res["benchmarks_full"][n]["yearly"][y], 1, "%") for n in ("SPY", "QQQ")])
    L.append(table(hdr, rows))
    L.append("")
    L.append(f"{years[0]} starts on {R['full | S1 as written']['start']}; {years[-1]} ends on {R['full | S1 as written']['end']}.")
    L.append("")

    # declines
    L.append("## During SPY's declines of 15% or more (full-period runs, peak close to trough close)")
    L.append("")
    hdr = ["SPY peak", "SPY trough", "SPY", "QQQ"] + [f"{s}, {lev}" for s, lev in VARIANTS]
    rows = [[d["peak"], d["trough"], f(d["SPY"], 1, "%"), f(d["QQQ"], 1, "%")] +
            [f(d[key(s, lev)], 1, "%", True) for s, lev in VARIANTS] for d in res["spy_declines"]]
    L.append(table(hdr, rows))
    L.append("")

    # vol matched
    L.append("## At the same volatility as SPY or QQQ (full period)")
    L.append("")
    L.append("The rules' daily excess returns over T-bills, scaled so the volatility equals the benchmark's over the "
             "whole period. The scale uses the full period's volatility, so it is a risk-adjusted comparison, not a "
             "tradable setting; it is close to changing the 1% risk per N to the multiple shown.")
    L.append("")
    hdr = ["", "Scale (x the 1% per N)", "CAGR", "Max drawdown", "Sharpe", "Benchmark CAGR / max drawdown"]
    rows = []
    for s, m in res["vol_matched"].items():
        for bn in ("spy", "qqq"):
            x = m[bn]
            bc = res["benchmarks_full"][bn.upper()]
            rows.append([f"{SYSTEM_NAMES[s]} at {bn.upper()}'s volatility", f"{x['k']:.2f}", f(x["cagr"], 1, "%"),
                         f(x["max_dd"], 1, "%"), f(x["sharpe"], 2), f"{f(bc['cagr'], 1, '%')} / {f(bc['max_dd'], 1, '%')}"])
    L.append(table(hdr, rows))
    L.append("")

    # sensitivity
    L.append("## Sensitivity (full period): CAGR / max drawdown / Sharpe")
    L.append("")
    combos = [("S1", "as written"), ("S2", "as written"), ("S1+S2", "as written"), ("S1+S2", "1x cap")]
    hdr = [""] + [f"{SYSTEM_NAMES[s]}, {lev}" for s, lev in combos]
    rows = []
    for label, _ in SENSITIVITY:
        rows.append([label] + [
            (lambda c: f"wiped out on {c['ruined_on']}" if c["ruined_on"] else
             f"{f(c['cagr'], 1, '%')} / {f(c['max_dd'], 0, '%')} / {f(c['sharpe'], 2)}")(
                res["sensitivity"][f"{label} | {s} {lev}"]) for s, lev in combos])
    L.append(table(hdr, rows))
    L.append("")

    # by market, exits, entries for the full as-written runs
    L.append("## Where the profit came from (full period, rules as written)")
    L.append("")
    hdr = ["Market"] + [SYSTEM_NAMES[s] for s in SYSTEMS]
    rows = []
    for sym in data.symbols:
        pnl = [R["full | " + key(s, "as written")]["trades"]["by_market"].get(sym, 0.0) for s in SYSTEMS]
        rows.append([sym] + [f"${x:,.0f}" for x in pnl])
    L.append(table(hdr, rows))
    L.append("")
    hdr = ["Unit exits"] + [SYSTEM_NAMES[s] for s in SYSTEMS]
    reasons = sorted({r for s in SYSTEMS for r in R[f"full | {key(s, 'as written')}"]["trades"]["exits"]})
    L.append(table(hdr, [[r] + [str(R[f"full | {key(s, 'as written')}"]["trades"]["exits"].get(r, 0)) for s in SYSTEMS]
                         for r in reasons]))
    L.append("")
    hdr = ["Entries"] + [SYSTEM_NAMES[s] for s in SYSTEMS]
    kinds = sorted({r for s in SYSTEMS for r in R[f"full | {key(s, 'as written')}"]["trades"]["entries"]})
    L.append(table(hdr, [[k + "-day breakout" if k.isdigit() else k + "-day breakout"] +
                         [str(R[f"full | {key(s, 'as written')}"]["trades"]["entries"].get(k, 0)) for s in SYSTEMS]
                         for k in kinds]))
    L.append("")
    hdr = ["Orders not placed"] + [f"{SYSTEM_NAMES[s]}, {lev}" for s, lev in VARIANTS]
    reasons = list(R[f"full | {key('S1', 'as written')}"]["blocked"])
    L.append(table(hdr, [[r] + [f"{R[f'full | {key(s, lev)}']['blocked'][r]:,}" for s, lev in VARIANTS] for r in reasons]))
    L.append("")
    L.append(METHOD)
    return "\n".join(L)


def findings(res: dict) -> str:
    R, B = res["runs"], res["benchmarks_full"]

    def c(k, p="full"):
        return R[f"{p} | {k}"]["curve"]

    s2, s1, half = c("S2 as written"), c("S1 as written"), c("S1+S2 as written")
    spy, qqq = B["SPY"], B["QQQ"]
    run2 = R["full | S2 as written"]
    tr2, ex2 = run2["trades"], run2["exposure"]
    thr = res["cagr_through"]["2024-12-31"]
    by = tr2["by_market"]
    best_mkt = max(by, key=by.get)
    gaps = [c(key(sy, lev), "combined")["cagr"] - res["benchmarks_combined"]["SPY"]["cagr"]
            for sy in SYSTEMS for lev in ("2x cap", "1x cap")]
    sens = res["sensitivity"]
    vm = res["vol_matched"]["S2"]["spy"]
    dl = "; ".join(f"{d['peak'][:7]} to {d['trough'][:7]}: SPY {d['SPY']:.0f}%, System 2 {d['S2 as written']:+.0f}%"
                   for d in res["spy_declines"])
    no_dd = sens["No drawdown rule | S2 as written"]
    no_dd_text = (f"was wiped out on {no_dd['ruined_on']}." if no_dd.get("ruined_on")
                  else f"had a {no_dd['max_dd']:.0f}% drawdown.")
    lines = [
        "## Findings",
        "",
        f"1. **As written, System 2 beat SPY and roughly matched QQQ, at three times the volatility.** From "
        f"{run2['start']}, $100,000 became ${s2['final']:,.0f} (SPY ${spy['final']:,.0f}, QQQ ${qqq['final']:,.0f}): "
        f"{s2['cagr']:.1f}%/yr against {spy['cagr']:.1f}% and {qqq['cagr']:.1f}%. Volatility {s2['vol']:.0f}% "
        f"(SPY {spy['vol']:.0f}%), max drawdown {s2['max_dd']:.0f}% (SPY {spy['max_dd']:.0f}%), worst day "
        f"{s2['worst_day']:.0f}% ({s2['worst_day_date']}), {s2['longest_dd_months']:.0f} months under a previous high "
        f"({s2['longest_dd_from']} to {s2['longest_dd_to']}). Sharpe {s2['sharpe']:.2f}, against SPY "
        f"{spy['sharpe']:.2f} and QQQ {qqq['sharpe']:.2f}.",
        f"2. **The margin over SPY came in 2025–26.** Through 2024, System 2 made {thr['S2 as written']:.1f}%/yr "
        f"against SPY's {thr['SPY']:.1f}% and QQQ's {thr['QQQ']:.1f}%. The 2025 gold and silver rallies and the 2026 "
        f"oil spike did the rest: {best_mkt} alone made ${by[best_mkt]:,.0f} of the ${tr2['total_pnl']:,.0f} trade "
        f"profit, and the best single trade was {tr2['best_share']:.0f}% of it.",
        f"3. **System 1 failed.** {s1['cagr']:.1f}%/yr with an {s1['max_dd']:.0f}% drawdown; without its "
        f"last-breakout filter, {sens['System 1 without the last-breakout filter | S1 as written']['cagr']:.1f}%/yr. "
        f"Half in each system made {half['cagr']:.1f}%/yr.",
        f"4. **The return depends on leverage these ETFs make extreme.** A unit is 1% of equity per N, and these ETFs "
        f"move much less per day than the Turtles' commodities, so one unit of SPY or TLT can be 1–2.5x equity. "
        f"System 2 averaged {ex2['avg_gross']:.1f}x gross exposure and reached {ex2['max_gross']:.1f}x. Capped at 2x "
        f"equity (Reg T margin) it made {c('S2 2x cap')['cagr']:.1f}%/yr with a {c('S2 2x cap')['max_dd']:.0f}% "
        f"drawdown; without borrowing, {c('S2 1x cap')['cagr']:.1f}%/yr with {c('S2 1x cap')['max_dd']:.0f}%. Since "
        f"2016 every capped version trails SPY, by {-max(gaps):.0f} to {-min(gaps):.0f} points a year.",
        f"5. **It works better as a diversifier than as a replacement.** Correlation to SPY was "
        f"{s2['corr_spy']:.2f} daily and {s2['corr_spy_monthly']:.2f} monthly. System 2 made money in every SPY "
        f"decline of 15% or more ({dl}). Scaled to SPY's volatility it would have made {vm['cagr']:.1f}%/yr with a "
        f"{vm['max_dd']:.0f}% drawdown, against SPY's {spy['cagr']:.1f}% and {spy['max_dd']:.0f}%.",
        f"6. **The trade profile is classic trend following.** Win rate {tr2['win_rate']:.0f}%, average win "
        f"{tr2['avg_win_r']:.1f}R against an average loss of {tr2['avg_loss_r']:.1f}R, expectancy "
        f"{tr2['expectancy']:+.2f}R ± {tr2['se']:.2f} ({tr2['exp_wo_top5']:+.2f}R without the five best trades), "
        f"a longest run of {tr2['losing_streak']} losing trades, and the top 10% of trades made "
        f"{tr2['top10_share']:.0f}% of the net profit. (The earlier Qullamaggie and Minervini tests had negative "
        f"expectancy.) Oil, gold and silver made the money; SPY and TLT lost it.",
        f"7. **Costs decide a lot.** The slippage assumption (0.10% a side, 0.25% when the price is under $20, which "
        f"caught SLV and USO for years) cost System 2 {-run2['flows_pct_per_year']['slippage']:.0f}% of equity a year. "
        f"At 0.05% a side it would have made "
        f"{sens['Slippage 0.05% per side, no low-price tier | S2 as written']['cagr']:.1f}%/yr; with no slippage, "
        f"{sens['No slippage | S2 as written']['cagr']:.1f}%.",
        f"8. **The drawdown rule kept the account alive.** Without the 20%-per-10% notional cuts, System 2 as "
        f"written {no_dd_text}",
        f"9. **The short side lost money.** Long-only, System 2 made {sens['Long only | S2 as written']['cagr']:.1f}%/yr. "
        f"That is an after-the-fact observation, not a tested rule.",
    ]
    return "\n".join(lines)


METHOD = """## Method

Rules (from the 2003 PDF):

- **N**: Wilder-style 20-day average true range, N = (19 × previous N + TR) / 20. The unit sheet is refreshed
  each Monday from the previous close; units, stops and add-on spacing use it.
- **Unit**: 1% of the notional account / N shares, so a 1N move in one unit is 1% of the account.
- **Limits**: 4 units per market; 6 in one direction across SPY+QQQ and across GLD+SLV (closely correlated); 12
  in one direction overall. No loosely correlated groups are defined among these six.
- **Entries**: System 1 buys one tick above the 20-day high (sells one tick below the 20-day low). It skips the
  signal when the previous 20-day breakout, taken or not, would have won; that breakout is followed as a
  one-unit trade with a 2N stop and the 10-day exit. A skipped signal leaves the 55-day breakout as the
  failsafe entry; while the previous breakout is still open, a flat System 1 also waits for the 55-day one.
  System 2 takes every 55-day breakout.
- **Adds**: one unit every ½N beyond the previous fill. **Stops**: 2N from each fill, with earlier stops raised
  ½N on each add. **Exits**: the opposite 10-day (System 1) or 20-day (System 2) breakout.
- **Notional account**: reset to equity each January; cut 20% for each 10% lost (at 10%, 18%, 24.4% … below the
  year's start), restored when equity regains the year's start.
- **Simultaneous signals**: markets are processed each day strongest first, by |63-session change| / N.
- **Half in each**: two separate accounts with $50,000 each, summed (the Turtles chose their own split).

Simulation on daily bars:

- Orders fill at the trigger, or at the open when the market opens beyond it. Within a session the price runs
  open, low, high, close on an up day and open, high, low, close on a down day; orders fire in the order the
  path reaches them, including orders created earlier that day. A gap through both a stop and the channel
  exit counts as a stop.
- Prices are dividend-adjusted daily bars, so longs earn the distributions and shorts pay them.
- Money: idle cash and short proceeds earn the 3-month T-bill yield; borrowed cash pays the yield + 0.5%;
  shorts pay 0.5% a year to borrow. This is close to the economics of the futures the Turtles traded: a futures
  position earns the asset's return over cash, and the margin deposit earns T-bill interest.
- Gross-exposure caps (2x, 1x) apply to long plus short value against the previous close's equity; units that
  do not fit shrink to the room left, and are skipped below a tenth of a unit.
- An account whose equity reaches zero at a close is liquidated there and stops (only the no-drawdown-rule
  sensitivity hit this).
- The simulator reproduces the rulebook's worked examples: the heating oil N table and unit size, the gold
  and crude oil add-on prices, the crude oil stop tables including the gap case, and the drawdown schedule.

Assumptions shared with the other backtests here: slippage 0.10% a side, 0.25% when the as-traded price is under $20 (SLV in 2008–2010
and 2013–2020, USO for several years before its 2020 reverse split); the $20M dollar-volume floor (all six ETFs
clear it on every session tested); SPY with dividends as the benchmark (QQQ added); each period is a separate
run from $100,000 with positions closed at its last close. The Turtle rules replace those backtests' 0.5% risk
sizing, 20% position cap, 10-position limit and no-margin default (kept here as the 1x variant).

Caveats:

- No parameter was fitted here: the rules date from 1983, and the market list and leverage variants were set
  before the runs. The in-sample and out-of-sample columns are plain sub-periods. The sensitivity rows were run
  afterwards and were not used to choose a headline.
- Six markets, two pairs of them closely correlated, give far less diversification than the Turtles' 20-odd
  futures. The ETFs' low volatility makes 1%-per-N units much larger relative to equity than they were on 1980s
  commodities.
- Daily bars cannot show the intraday order of highs and lows beyond the assumption above. Stops fill at their
  price plus the assumed slippage; fast-market fills are not modeled.
- The ETFs' fees and USO's futures roll costs are in their prices, as they would be for a holder.
- Files: trade lists in `trades/<period>_<system>_<leverage>.csv`, daily values in `curves_full.csv`.
"""


if __name__ == "__main__":
    main()
