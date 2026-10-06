"""The research runner, ledger and reports. The simulation, rules and metrics themselves live in
stratlib.sim.engine, shared with the app's backtests; they are imported here under their old names."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from stratlib.sim.engine import (  # noqa: F401  (re-exported for the research scripts)
    PERIODS,
    PERIOD_TITLES,
    Rules,
    Position,
    Trade,
    Strategy,
    SETUPS_KEPT,
    _SETUPS,
    set_up,
    _freeze,
    _share,
    slippage,
    entry_day_stop_hit,
    market_series,
    simulate,
    spy_series,
    drawdown,
    cagr,
    sharpe,
    _ffill,
    yearly,
    metrics,
)
from panel import Panel

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"

# ----------------------------------------------------------------------
# Test runner: in-sample, out-of-sample (once), combined


def params_hash(strategy_name: str, params: dict, rules: Rules) -> str:
    blob = json.dumps({"strategy": strategy_name, "params": params, "rules": asdict(rules)}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def same_test(ledger: dict, strategy_name: str, params: dict, rules: Rules) -> bool:
    """Same strategy, parameters and rules as the ledger. A rule field added to Rules after the ledger
    was written counts as unchanged when it is at its default value."""
    if ledger.get("strategy") != strategy_name or json.loads(json.dumps(params)) != ledger.get("params"):
        return False
    stored, defaults = ledger.get("rules", {}), asdict(Rules())
    return all(stored[k] == v if k in stored else v == defaults[k] for k, v in asdict(rules).items())


def sweep(panel: Panel, make_strategy, grid: list[dict], benchmarks: dict, rules: Rules | None = None) -> list[dict]:
    """In-sample only: evaluate each parameter set on 2016-2021."""
    rules = rules or Rules()
    start, end = PERIODS["in_sample"]
    spy = spy_series(panel, benchmarks["spy_dividends"], start, end)
    rows = []
    for params in grid:
        strategy = set_up(make_strategy, panel, params)
        m = metrics(simulate(panel, strategy, start, end, rules), spy, benchmarks["tbill3m"])
        rows.append({"params": params, **{k: m[k] for k in ("trades", "win_rate", "expectancy_r", "profit_factor",
                                                            "cagr", "max_drawdown", "sharpe", "exposure_time_pct")}})
    return rows


def run_test(panel: Panel, name: str, make_strategy, params: dict, benchmarks: dict, *, rules: Rules | None = None,
             description: str = "", allow_oos_rerun: bool = False, periods: dict | None = None) -> dict:
    """Run the three periods (or `periods`, {name: (start, end)}) with frozen parameters and write the
    report and trade lists.

    The out-of-sample period may run only once per test name. A second call
    with different parameters or rules is refused; the same parameters
    reproduce the same result and are allowed.
    """
    rules = rules or Rules()
    folder = OUTPUT / name
    folder.mkdir(parents=True, exist_ok=True)
    ledger_path = folder / "ledger.json"
    digest = params_hash(make_strategy().name, params, rules)
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger["hash"] != digest and not same_test(ledger, make_strategy().name, params, rules) and not allow_oos_rerun:
            raise RuntimeError(f"{name}: the out-of-sample period already ran with other parameters "
                               f"({ledger['frozen_at']}). Use a new test name.")
    else:
        ledger_path.write_text(json.dumps({"hash": digest, "frozen_at": datetime.now().isoformat(timespec="seconds"),
                                           "strategy": make_strategy().name, "params": params,
                                           "rules": asdict(rules)}, indent=2), encoding="utf-8")
    last = str(panel.dates[-1])
    results = {}
    periods = periods or PERIODS
    for period, (start, end) in periods.items():
        strategy = set_up(make_strategy, panel, params)
        if hasattr(strategy, "begin_period"):
            strategy.begin_period(period)
        market = market_series(panel, benchmarks) if (rules.idle_cash_tbill or rules.overlay_spy) else None
        run = simulate(panel, strategy, start, end, rules, market)
        spy = spy_series(panel, benchmarks["spy_dividends"], start, end or last)
        results[period] = metrics(run, spy, benchmarks["tbill3m"])
        write_trades(folder / f"trades_{period}.csv", run["trades"])
        curve = results[period]["equity_curve"]
        with (folder / f"equity_{period}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "equity", "spy_with_dividends", "spy_price"])
            w.writerows(zip(curve["dates"], *(np.round(curve[k], 2) for k in ("equity", "spy_total", "spy_price"))))
    summary = {"name": name, "strategy": make_strategy().name, "description": description, "params": params,
               "rules": asdict(rules), "data_through": last, "panel_notes": {k: v for k, v in panel.notes.items()
                                                                            if k != "split_history_missing"},
               "periods": {k: list(v) for k, v in periods.items()}, "results": results}
    (folder / "results.json").write_text(json.dumps(summary, indent=1, default=float), encoding="utf-8")
    (folder / "report.md").write_text(report(summary), encoding="utf-8")
    return summary


def write_trades(path: Path, trades: list[Trade]) -> None:
    columns = ["entry_date", "exit_date", "ticker", "entry_price", "stop", "exit_price", "exit_reason",
               "return_pct", "r", "kind", "signal_date", "shares", "position_value", "pnl", "holding_days",
               "holding_sessions", "entry_price_as_traded", "exit_price_as_traded", "cash_limited", "entry_note",
               "partial_date", "partial_exit_price", "final_exit_price"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for tr in trades:
            row = asdict(tr)
            for k in ("entry_price", "stop", "exit_price", "entry_price_as_traded", "exit_price_as_traded",
                      "partial_exit_price", "final_exit_price"):
                row[k] = round(row[k], 4) if row[k] is not None else ""
            for k in ("return_pct", "r", "pnl", "position_value", "shares"):
                row[k] = round(row[k], 4)
            w.writerow({c: row[c] for c in columns})


# ----------------------------------------------------------------------
# Report


def top_share(m: dict) -> str:
    """Share of net profit from the top 10% of trades; in dollars when the net result is a loss."""
    n = m["top10pct_trades"]
    label = f"{n} {'trade' if n == 1 else 'trades'}"
    if math.isfinite(m.get("top10pct_profit_share", float("nan"))):
        return f"{_f(m['top10pct_profit_share'], 0, '%')} ({label})"
    return f"net loss: top {label} made ${m['top10pct_pnl']:,.0f}, the rest ${m['rest_pnl']:,.0f}"


def _f(x, digits=1, suffix=""):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–" if not (isinstance(x, float) and math.isinf(x)) else "∞"
    return f"{x:,.{digits}f}{suffix}"


def report(summary: dict) -> str:
    res = summary["results"]
    order = list(summary.get("periods", PERIODS))
    lines = [f"# {summary['name']}", "", summary.get("description", ""), "",
             f"Strategy `{summary['strategy']}`, parameters `{json.dumps(summary['params'])}`. "
             f"Data through {summary['data_through']}.", ""]
    head = "| | " + " | ".join(PERIOD_TITLES.get(p, p) for p in order) + " |"
    lines += [head, "|---|" + "---|" * len(order)]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(res[p]) for p in order) + " |")

    row("Period", lambda m: f"{m['start']} to {m['end']}")
    row("Trades", lambda m: f"{m['trades']:,}")
    row("Win rate", lambda m: _f(m["win_rate"], 1, "%"))
    row("Average win", lambda m: f"{_f(m['avg_win_pct'], 2, '%')} / {_f(m['avg_win_r'], 2, 'R')}")
    row("Average loss", lambda m: f"{_f(m['avg_loss_pct'], 2, '%')} / {_f(m['avg_loss_r'], 2, 'R')}")
    row("Expectancy", lambda m: _f(m["expectancy_r"], 3, "R"))
    row("Profit factor", lambda m: _f(m["profit_factor"], 2))
    row("Total return", lambda m: _f(m["total_return"], 1, "%"))
    row("CAGR", lambda m: _f(m["cagr"], 1, "%"))
    row("Max drawdown", lambda m: _f(m["max_drawdown"], 1, "%"))
    row("Sharpe", lambda m: _f(m["sharpe"], 2))
    row("Average holding", lambda m: f"{_f(m['avg_holding_days'], 1)} days ({_f(m['avg_holding_sessions'], 1)} sessions)")
    row("Exposure: time with a position", lambda m: _f(m["exposure_time_pct"], 1, "%"))
    row("Exposure: average invested", lambda m: _f(m["exposure_avg_invested_pct"], 1, "%"))
    row("Top 10% of trades, share of profit", top_share)
    row("SPY total return (with dividends)", lambda m: _f(m["spy"]["total_return"], 1, "%"))
    row("SPY price return", lambda m: _f(m["spy"]["price_return"], 1, "%"))
    row("SPY CAGR (with dividends)", lambda m: _f(m["spy"]["cagr"], 1, "%"))
    row("SPY max drawdown", lambda m: _f(m["spy"]["max_drawdown"], 1, "%"))
    row("SPY Sharpe", lambda m: _f(m["spy"]["sharpe"], 2))
    longest = max(order, key=lambda p: len(res[p]["yearly"]))
    lines += ["", "## Yearly returns", "",
              "Each period is its own run from fresh capital, so the same year can differ between runs.", "",
              "| Year | " + " | ".join(f"{PERIOD_TITLES.get(p, p)} run" for p in order)
              + " | SPY with dividends | SPY price |", "|---|" + "---|" * (len(order) + 2)]
    for y in res[longest]["yearly"]:
        cells = [_f(res[p]["yearly"][y], 1, "%") if y in res[p]["yearly"] else "" for p in order]
        lines.append(f"| {y} | " + " | ".join(cells) + f" | {_f(res[longest]['spy']['yearly'][y], 1, '%')} | "
                     f"{_f(res[longest]['spy']['yearly_price'][y], 1, '%')} |")
    lines += ["", "## Exits and signals", "", "| | " + " | ".join(PERIOD_TITLES.get(p, p) for p in order) + " |",
              "|---|" + "---|" * len(order)]
    reasons = sorted({r for p in order for r in res[p]["exit_reasons"]})
    for r in reasons:
        row(f"Exit: {r}", lambda m, r=r: f"{m['exit_reasons'].get(r, 0):,}")
    labels = {"signals": "Signals (new setups)", "triggered": "Breakouts through the trigger",
              "not_confirmed": "Breakouts not confirmed", "entries": "Entries",
              "skipped_no_slot": "Skipped: no free slot", "skipped_stop_rule": "Skipped: stop rule",
              "skipped_no_cash": "Skipped: no cash", "skipped_already_held": "Skipped: stock already held",
              "cash_limited_entries": "Entries shrunk to the cash available",
              "entry_day_stop_exits": "Stopped out on the entry day", "gap_stop_exits": "Stops gapped through"}
    keys = [k for k in labels if any(k in res[p]["counts"] for p in order)]
    for k in keys:
        row(labels[k], lambda m, k=k: f"{m['counts'].get(k, 0):,}")
    n = summary.get("panel_notes", {})
    if n:
        cleaning = n.get("cleaning", {})
        lines += ["", "## Data", "",
                  f"- {n.get('securities', 0):,} securities reached the liquidity floor at least once: "
                  f"{n.get('stocks', 0):,} stocks ({n.get('delisted_stocks', 0):,} delisted) and "
                  f"{n.get('etfs', 0):,} ETFs ({n.get('delisted_etfs', 0):,} delisted).",
                  "- Delisted coverage is thin before 2021 (the price data has histories for only 28 stocks delisted "
                  "in 2019 and 9 in 2020), so the in-sample period is flattered by survivorship more than "
                  "the out-of-sample period.",
                  f"- {len(n.get('duplicates', [])):,} groups of tickers carrying the same history (renames) were "
                  f"merged. Bad bars removed: {cleaning.get('spike_bars_removed', 0):,} one-day spikes, "
                  f"{cleaning.get('interleaved_bars_removed', 0):,} in interleaved series, "
                  f"{cleaning.get('final_bars_removed', 0)} final bars; "
                  f"{len(cleaning.get('instrument_changes', []))} tickers end where an acquired company's ticker "
                  "turned into another series.",
                  "- Trade prices are split-adjusted and include slippage. Trade returns exclude dividends."]
    return "\n".join(lines) + "\n"
