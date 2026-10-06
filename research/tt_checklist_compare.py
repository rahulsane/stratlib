"""Compare the checklist portfolio constructions (tt_checklist_fwd.py variants) on one page.

Reads each test's results.json, equity and trade files from research/output/<test>/ and adds risk and
concentration measures the engine report does not show: annualized volatility, Calmar (CAGR / max
drawdown), beta and correlation to SPY, tracking error and information ratio, worst month, longest time
under a previous high, share of rolling 12-month windows and of calendar years beating SPY, positions held
(average and most), initial position weights (average and largest), turnover (value bought per year over
average equity), trades per year, and the best single trade's share of total profit.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import OUTPUT, PERIOD_TITLES  # noqa: E402
import tt_data as T  # noqa: E402

TESTS = [
    ("tt_checklist_fwd", "A: 5% positions, idle cash"),
    ("tt_checklist_fwd_spy", "B: 5% positions, rest in SPY"),
    ("tt_checklist_fwd_ew", "C: equal weight, fully invested"),
    ("tt_checklist_qual_ew", "D: C without the forward tests (no hindsight)"),
]
PERIODS = ["combined", "out_of_sample", "in_sample"]


def load(test: str) -> dict:
    folder = OUTPUT / test
    res = json.loads((folder / "results.json").read_text(encoding="utf-8"))["results"]
    trades = {}
    for period in PERIODS:
        with (folder / f"trades_{period}.csv").open(encoding="utf-8") as f:
            trades[period] = [{**r, "r": float(r["r"]), "pnl": float(r["pnl"]), "position_value": float(r["position_value"]),
                               "holding_days": int(r["holding_days"])} for r in csv.DictReader(f)]
    return {"results": res, "trades": trades}


def curve_stats(curve: dict, capital: float) -> dict:
    dates = np.array(curve["dates"])
    eq = np.concatenate([[capital], np.array(curve["equity"])])
    spy = np.concatenate([[capital], np.array(curve["spy_total"])])
    d = np.concatenate([[dates[0]], dates])
    r, rs = eq[1:] / eq[:-1] - 1, spy[1:] / spy[:-1] - 1
    years = (T.date.fromisoformat(str(d[-1])) - T.date.fromisoformat(str(d[0]))).days / 365.25
    vol = 100 * r.std(ddof=1) * np.sqrt(252)
    active = r - rs
    te = 100 * active.std(ddof=1) * np.sqrt(252)
    beta = float(np.cov(r, rs)[0, 1] / rs.var(ddof=1)) if rs.var(ddof=1) > 0 else np.nan
    corr = float(np.corrcoef(r, rs)[0, 1])
    cagr = 100 * ((eq[-1] / eq[0]) ** (1 / years) - 1)
    cagr_spy = 100 * ((spy[-1] / spy[0]) ** (1 / years) - 1)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1
    max_dd = 100 * dd.min()
    # longest stretch under a previous high, in sessions
    longest, run = 0, 0
    for x in dd:
        run = run + 1 if x < 0 else 0
        longest = max(longest, run)
    # monthly returns
    month_end = [i for i in range(len(d) - 1) if str(d[i])[:7] != str(d[i + 1])[:7]] + [len(d) - 1]
    m_eq, m_spy = eq[month_end], spy[month_end]
    mr, ms = m_eq[1:] / m_eq[:-1] - 1, m_spy[1:] / m_spy[:-1] - 1
    roll = eq[252:] / eq[:-252] - 1
    roll_spy = spy[252:] / spy[:-252] - 1
    return {"cagr": cagr, "cagr_spy": cagr_spy, "vol": vol, "vol_spy": 100 * rs.std(ddof=1) * np.sqrt(252),
            "calmar": cagr / abs(max_dd) if max_dd < 0 else np.nan, "max_dd": max_dd,
            "longest_dd_months": longest / 21, "beta": beta, "corr": corr, "tracking_error": te,
            "info_ratio": (cagr - cagr_spy) / te if te > 0 else np.nan,
            "worst_month": 100 * mr.min(), "worst_month_spy": 100 * ms.min(), "months_beat_spy": 100 * float((mr > ms).mean()),
            "roll12_beat_spy": 100 * float((roll > roll_spy).mean()) if len(roll) else np.nan,
            "roll12_worst": 100 * float(roll.min()) if len(roll) else np.nan, "years": years,
            "dates": d, "equity": eq}


def trade_stats(trades: list[dict], dates: np.ndarray, equity: np.ndarray, years: float) -> dict:
    rs = np.array([t["r"] for t in trades if np.isfinite(t["r"])])
    if not len(rs):
        return {}
    order = sorted(rs, reverse=True)
    rest = np.array(order[5:])
    pnl = np.array([t["pnl"] for t in trades])
    total = pnl.sum()
    idx = {str(d): i for i, d in enumerate(dates)}
    open_count = np.zeros(len(dates))
    weights = []
    for t in trades:
        a, b = idx.get(t["entry_date"]), idx.get(t["exit_date"])
        if a is None or b is None:
            continue
        open_count[a:b + 1] += 1
        weights.append(100 * t["position_value"] / equity[a])
    bought = sum(t["position_value"] for t in trades)
    return {"trades": len(trades), "per_year": len(trades) / years, "win_rate": 100 * float((pnl > 0).mean()),
            "expectancy": float(rs.mean()), "se": float(rs.std(ddof=1) / np.sqrt(len(rs))),
            "exp_wo_top5": float(rest.mean()) if len(rest) else np.nan,
            "best_share": 100 * pnl.max() / total if total > 0 else np.nan,
            "hold": float(np.mean([t["holding_days"] for t in trades])),
            "avg_open": float(open_count[open_count > 0].mean()) if (open_count > 0).any() else 0.0,
            "max_open": int(open_count.max()), "avg_weight": float(np.mean(weights)) if weights else np.nan,
            "max_weight": float(np.max(weights)) if weights else np.nan,
            "turnover": 100 * bought / equity.mean() / years}


ROWS = [
    ("CAGR", lambda m, c, t: f"{c['cagr']:.1f}%"),
    ("SPY CAGR (dividends)", lambda m, c, t: f"{c['cagr_spy']:.1f}%"),
    ("CAGR minus SPY", lambda m, c, t: f"{c['cagr'] - c['cagr_spy']:+.1f} pts"),
    ("Annualized volatility (SPY)", lambda m, c, t: f"{c['vol']:.1f}% ({c['vol_spy']:.1f}%)"),
    ("Sharpe (SPY)", lambda m, c, t: f"{m['sharpe']:.2f} ({m['spy']['sharpe']:.2f})"),
    ("Max drawdown (SPY)", lambda m, c, t: f"{c['max_dd']:.1f}% ({m['spy']['max_drawdown']:.1f}%)"),
    ("Calmar: CAGR / max drawdown", lambda m, c, t: f"{c['calmar']:.2f}"),
    ("Longest time under a previous high", lambda m, c, t: f"{c['longest_dd_months']:.0f} months"),
    ("Worst month (SPY)", lambda m, c, t: f"{c['worst_month']:+.1f}% ({c['worst_month_spy']:+.1f}%)"),
    ("Worst rolling 12 months", lambda m, c, t: f"{c['roll12_worst']:+.1f}%"),
    ("Beta / correlation to SPY", lambda m, c, t: f"{c['beta']:.2f} / {c['corr']:.2f}"),
    ("Tracking error / information ratio", lambda m, c, t: f"{c['tracking_error']:.1f}% / {c['info_ratio']:.2f}"),
    ("Calendar years beating SPY", lambda m, c, t: f"{sum(m['yearly'][y] > m['spy']['yearly'][y] for y in m['yearly'])} of {len(m['yearly'])}"),
    ("Rolling 12-month windows beating SPY", lambda m, c, t: f"{c['roll12_beat_spy']:.0f}%"),
    ("Months beating SPY", lambda m, c, t: f"{c['months_beat_spy']:.0f}%"),
    ("Average invested / time with a position", lambda m, c, t: f"{m['exposure_avg_invested_pct']:.0f}% / {m['exposure_time_pct']:.0f}%"),
    ("Positions held: average / most", lambda m, c, t: f"{t['avg_open']:.1f} / {t['max_open']}"),
    ("Initial position weight: average / largest", lambda m, c, t: f"{t['avg_weight']:.0f}% / {t['max_weight']:.0f}%"),
    ("Turnover (bought per year / equity)", lambda m, c, t: f"{t['turnover']:.0f}%"),
    ("Trades / per year", lambda m, c, t: f"{t['trades']} / {t['per_year']:.0f}"),
    ("Win rate", lambda m, c, t: f"{t['win_rate']:.0f}%"),
    ("Expectancy (R) ± SE", lambda m, c, t: f"{t['expectancy']:+.2f} ± {t['se']:.2f}"),
    ("Expectancy without the top 5", lambda m, c, t: f"{t['exp_wo_top5']:+.2f}"),
    ("Average holding (days)", lambda m, c, t: f"{t['hold']:.0f}"),
    ("Top 10% of trades, share of profit", lambda m, c, t: f"{m['top10pct_profit_share']:.0f}%" if np.isfinite(m['top10pct_profit_share']) else "n/a"),
    ("Best single trade, share of profit", lambda m, c, t: f"{t['best_share']:.0f}%" if np.isfinite(t['best_share']) else "n/a"),
]


def main() -> None:
    data = {test: load(test) for test, _ in TESTS}
    lines = ["# Checklist portfolio constructions compared", "",
             "Same screen dates, same 20% stop and sell-on-failure exit. A, B and C use the forward-proxy checklist "
             "(hindsight in the earnings estimates); D uses the hindsight-free checklist with C's sizing. "
             "Definitions: docstring of `tt_checklist_compare.py`.", ""]
    out = {}
    for period in PERIODS:
        lines += [f"## {PERIOD_TITLES[period]}", "", "| | " + " | ".join(label for _, label in TESTS) + " |",
                  "|---|" + "---|" * len(TESTS)]
        cells = {}
        for test, label in TESTS:
            m = data[test]["results"][period]
            c = curve_stats(m["equity_curve"], 100_000.0)
            t = trade_stats(data[test]["trades"][period], c["dates"], c["equity"], c["years"])
            cells[test] = (m, c, t)
            out[f"{test} {period}"] = {k: v for k, v in c.items() if k not in ("dates", "equity")} | t
        for title, fn in ROWS:
            lines.append(f"| {title} | " + " | ".join(fn(*cells[test]) for test, _ in TESTS) + " |")
        lines.append("")
    lines += ["## Yearly returns (combined runs)", "", "| year | SPY | " + " | ".join(label for _, label in TESTS) + " |",
              "|---|---|" + "---|" * len(TESTS)]
    first = data[TESTS[0][0]]["results"]["combined"]
    for y in first["yearly"]:
        lines.append(f"| {y} | {first['spy']['yearly'][y]:+.1f}% | " + " | ".join(
            f"{data[test]['results']['combined']['yearly'][y]:+.1f}%" for test, _ in TESTS) + " |")
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "checklist_compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump(out, open(T.OUT / "checklist_compare.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
