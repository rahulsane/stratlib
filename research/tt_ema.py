"""Test 2: the 9/21 EMA pullback trade under the ground rules, plus a signal-level event study.

Both exit rules were specified in advance (strategies/ema_pullback.py), so each runs the three periods once
through the engine (0.5% risk, at most 10 positions ranked by 63-session return, slippage, SPY benchmark).

Event study (no portfolio limits): every eligible signal is bought at the close with 0.10% slippage, held with
the 3%-below-EMA stop and the "first close below the 21-day EMA" exit, capped at 252 sessions, sold with
0.10% slippage. Reported per period: signals, win rate, mean and median return, mean excess return over SPY
across the same sessions, mean holding. Overlapping trades make the t-statistics optimistic.
Touch vs trend: on each session, the mean 21-session forward return of the day's signals minus that of every
eligible stock that passes the trend and trending-stock filters (whether or not it touched the EMA).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_rules as rr  # noqa: E402
from engine import PERIOD_TITLES, PERIODS, Rules, _f, run_test, top_share  # noqa: E402
from lab import load_all  # noqa: E402
from strategies.ema_pullback import DEFAULTS, EmaPullback, ema, rolling  # noqa: E402
import tt_data as T  # noqa: E402

VARIANTS = [
    ("tt_ema_first", {"exit_rule": "first"}, "exit at the first close below the 21-day EMA"),
    ("tt_ema_second", {"exit_rule": "second"}, "exit at the second close below the 21-day EMA"),
]
ROWS = [
    ("Trades", lambda m: f"{m['trades']:,}"),
    ("Win rate", lambda m: _f(m["win_rate"], 1, "%")),
    ("Average win", lambda m: f"{_f(m['avg_win_pct'], 2, '%')} / {_f(m['avg_win_r'], 2, 'R')}"),
    ("Average loss", lambda m: f"{_f(m['avg_loss_pct'], 2, '%')} / {_f(m['avg_loss_r'], 2, 'R')}"),
    ("Expectancy", lambda m: _f(m["expectancy_r"], 3, "R")),
    ("Profit factor", lambda m: _f(m["profit_factor"], 2)),
    ("Total return", lambda m: _f(m["total_return"], 1, "%")),
    ("CAGR", lambda m: _f(m["cagr"], 1, "%")),
    ("Max drawdown", lambda m: _f(m["max_drawdown"], 1, "%")),
    ("Sharpe", lambda m: _f(m["sharpe"], 2)),
    ("Average holding (days)", lambda m: _f(m["avg_holding_days"], 1)),
    ("Time with a position", lambda m: _f(m["exposure_time_pct"], 1, "%")),
    ("Average invested", lambda m: _f(m["exposure_avg_invested_pct"], 1, "%")),
    ("Top 10% of trades, share of profit", top_share),
    ("SPY total return (with dividends)", lambda m: _f(m["spy"]["total_return"], 1, "%")),
    ("SPY CAGR (with dividends)", lambda m: _f(m["spy"]["cagr"], 1, "%")),
]
SLIP = 0.001
MAX_HOLD = 252


def event_study(panel, strat: EmaPullback) -> dict:
    p = panel
    spy_j = int(np.flatnonzero(p.symbols == "SPY")[0])
    spy = p.close[:, spy_j]
    n_days = len(p.dates)
    signals = np.argwhere(strat.setups & p.eligible & (p.kind == "stock")[None, :])
    rows = []
    for t, j in signals:
        if t + 1 >= n_days:
            continue
        entry = p.close[t, j] * (1 + SLIP)
        stop = strat.ema_slow[t, j] * (1 - DEFAULTS["stop_pct"] / 100)
        last = min(int(p.last_bar[j]), t + MAX_HOLD, n_days - 1)
        exit_price, exit_t, reason = None, last, "cap"
        for s in range(t + 1, last + 1):
            o, lo, c, e = p.open[s, j], p.low[s, j], p.close[s, j], strat.ema_slow[s, j]
            if not np.isfinite(c):
                continue
            if np.isfinite(o) and o <= stop:
                exit_price, exit_t, reason = o, s, "gap below stop"
                break
            if np.isfinite(lo) and lo <= stop:
                exit_price, exit_t, reason = stop, s, "stop"
                break
            if np.isfinite(e) and c < e:
                exit_price, exit_t, reason = c, s, "close below EMA"
                break
        if exit_price is None:
            k = exit_t
            while k > t and not np.isfinite(p.close[k, j]):
                k -= 1
            if k == t:
                continue
            exit_price, exit_t = p.close[k, j], k
        ret = exit_price * (1 - SLIP) / entry - 1
        spy_ret = spy[exit_t] / spy[t] - 1 if np.isfinite(spy[t]) and np.isfinite(spy[exit_t]) else np.nan
        rows.append((str(p.dates[t]), int(j), ret, spy_ret, exit_t - t, reason))
    # touch vs trend universe, 21-session forward returns
    fwd = np.full_like(p.close, np.nan)
    fwd[:-21] = p.close_ff[21:] / p.close[:-21] - 1
    with np.errstate(invalid="ignore"):
        c = p.close
        sma_mid = rolling(c, DEFAULTS["sma_mid"], "mean")
        sma_long = rolling(c, DEFAULTS["sma_long"], "mean")
        trend = (strat.ema_fast > strat.ema_slow) & (strat.ema_slow > sma_mid) & (c > sma_long)
        trending = rolling(c, DEFAULTS["high_recent"], "max") >= rolling(c, DEFAULTS["high_lookback"], "max")
    universe = p.valid & trend & trending & p.eligible & (p.kind == "stock")[None, :]
    spreads = []
    for t in range(n_days - 21):
        sig = strat.setups[t] & universe[t]
        if sig.sum() >= 3 and universe[t].sum() >= 20:
            a, b = fwd[t, sig], fwd[t, universe[t]]
            if np.isfinite(a).sum() >= 3:
                spreads.append((str(p.dates[t]), float(np.nanmean(a) - np.nanmean(b)), int(sig.sum()), int(universe[t].sum())))
    return {"trades": rows, "spreads": spreads}


def period_stats(rows, spreads, a: str, b: str) -> dict:
    sel = [r for r in rows if a <= r[0] <= b]
    if not sel:
        return {}
    ret = np.array([r[2] for r in sel])
    exc = np.array([r[2] - r[3] for r in sel if np.isfinite(r[3])])
    hold = np.array([r[4] for r in sel])
    reasons = {}
    for r in sel:
        reasons[r[5]] = reasons.get(r[5], 0) + 1
    sp = np.array([s[1] for s in spreads if a <= s[0] <= b])
    out = {"signals": len(sel), "win_rate": 100 * float((ret > 0).mean()), "mean": 100 * float(ret.mean()),
           "median": 100 * float(np.median(ret)), "avg_win": 100 * float(ret[ret > 0].mean()) if (ret > 0).any() else np.nan,
           "avg_loss": 100 * float(ret[ret <= 0].mean()) if (ret <= 0).any() else np.nan,
           "excess_mean": 100 * float(exc.mean()), "excess_t": float(exc.mean() / (exc.std(ddof=1) / np.sqrt(len(exc)))),
           "hold_mean": float(hold.mean()), "reasons": reasons,
           "touch_vs_trend_mean": 100 * float(sp.mean()) if len(sp) else np.nan,
           "touch_vs_trend_t": float(sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp)))) if len(sp) > 2 else np.nan,
           "touch_days": int(len(sp))}
    return out


STRATEGY = [
    "The 9/21 EMA pullback is the Traveling Trader's trend trade: buy a strong stock when it dips into its 9- and "
    "21-day exponential moving averages (EMA), and sell when it closes below the 21-day. A stock signals at the "
    "close of session T when:",
    "",
    "1. Trend. The 9-day EMA is above the 21-day EMA, the 21-day EMA is above the 50-day SMA, and the close is above "
    "the 200-day SMA.",
    "2. Trending stock. The highest close of the last 20 sessions is also the highest close of the last 126.",
    "3. Pullback touch. The session's low reaches the 9-day EMA while the close holds at or above the 21-day EMA, "
    "and the lows of the previous three sessions were all above the 9-day EMA, so this is a fresh pullback.",
    "",
    "Entry: buy at T's close. Initial stop: 3% below the 21-day EMA at entry, never moved.",
    "",
    "Exits, both fixed before any results:",
    "",
    "- First close: sell at the first close below the 21-day EMA.",
    "- Second close: sell at the second close below the 21-day EMA, allowing one break and retest. The count resets "
    "after 10 sessions back above it.",
]


def rules_section() -> list[str]:
    return rr.section(
        ["This report runs the 9/21 EMA pullback as a portfolio under the portfolio rules below, with two exits, then "
         "studies every signal on its own."],
        STRATEGY,
        rr.GROUND_RULES,
        ["The event study takes every signal from 2016 on, traded or not, with no portfolio limits. Each is bought at "
         "the close with 0.10% slippage, held with the 3% stop and the first-close exit for at most 252 sessions, and "
         "sold with 0.10% slippage. *Excess vs SPY* subtracts SPY's return over the same sessions. *Touch vs trend "
         "universe* compares, day by day, the next 21 sessions' return of that day's signals with that of every liquid "
         "stock passing conditions 1 and 2, touch or not; it gives the average daily difference, its t and the number "
         "of days. Trades overlap in time, so the t-statistics overstate the evidence."],
        rr.TERMS,
    )


def main() -> None:
    panel, bench = load_all()
    results = {}
    for name, params, label in VARIANTS:
        summary = run_test(panel, name, EmaPullback, {**DEFAULTS, **params}, bench,
                           description="\n".join([f"{label[0].upper()}{label[1:]}.", "", *STRATEGY]))
        results[name] = (label, summary["results"])
        print(f"{name}: done", flush=True)
    strat = EmaPullback()
    strat.setup(panel, DEFAULTS)
    ev = event_study(panel, strat)
    periods = {"in-sample 2016-21": ("2016-01-01", "2021-12-31"), "2022 on": ("2022-01-01", "9999-12-31"),
               "combined 2016 on": ("2016-01-01", "9999-12-31")}
    ev_stats = {k: period_stats(ev["trades"], ev["spreads"], a, b) for k, (a, b) in periods.items()}

    lines = ["# The 9/21 EMA pullback trade", "", f"Data through {panel.dates[-1]}.", "",
             *rules_section()]
    for period in PERIODS:
        lines += [f"## {PERIOD_TITLES[period]}", "", "| | " + " | ".join(label for label, _ in results.values()) + " |",
                  "|---|" + "---|" * len(results)]
        for title, fn in ROWS:
            lines.append(f"| {title} | " + " | ".join(fn(res[period]) for _, res in results.values()) + " |")
        lines.append("")
    lines += ["## Yearly returns (combined runs)", "", "| Year | " + " | ".join(label for label, _ in results.values())
              + " | SPY with dividends |", "|---|" + "---|" * (len(results) + 1)]
    first = next(iter(results.values()))[1]["combined"]
    for year in first["yearly"]:
        cells = [_f(res["combined"]["yearly"][year], 1, "%") for _, res in results.values()]
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {_f(first['spy']['yearly'][year], 1, '%')} |")
    lines += ["", "## Event study: every eligible signal, first-close-below-EMA exit, no portfolio limits", "",
              "| period | signals | win rate | mean | median | avg win | avg loss | excess vs SPY (t) | holding | touch vs trend universe, 21 sessions (t, days) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in ev_stats.items():
        if s:
            lines.append(f"| {k} | {s['signals']:,} | {s['win_rate']:.1f}% | {s['mean']:+.2f}% | {s['median']:+.2f}% | "
                         f"{s['avg_win']:+.2f}% | {s['avg_loss']:+.2f}% | {s['excess_mean']:+.2f}% ({s['excess_t']:+.1f}) | "
                         f"{s['hold_mean']:.1f} | {s['touch_vs_trend_mean']:+.2f}% ({s['touch_vs_trend_t']:+.1f}, {s['touch_days']}) |")
    s = ev_stats["combined 2016 on"]
    if s:
        lines += ["", "Exit reasons (combined): " + ", ".join(f"{k} {v:,}" for k, v in sorted(s["reasons"].items(), key=lambda x: -x[1])), ""]
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "ema.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump({"portfolio": {k: v[1] for k, v in results.items()}, "event_study": ev_stats},
              open(T.OUT / "ema.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
