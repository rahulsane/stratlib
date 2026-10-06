"""Test 3b: the checklist with forward-estimate proxies, as a screen and as a portfolio. Rules fixed before
any results were seen.

Forward proxies, from per-quarter earnings histories (cached by research/earnings.py; EPS on today's
split basis, like the panel's closes):
  NTM EPS at a date = the sum of the consensus EPS on the next four report dates after it. Each consensus is
      the one that stood just before its own report, so the figure embeds up to 12 months of hindsight: this
      is an upper bound for the rule, and only a failure is conclusive. All four estimates must exist and
      the fourth report must fall within 15 months.
  TTM EPS = the sum of the actual EPS of the last four reports on or before the date (all four present, the
      oldest within 15 months).
  FWDG    NTM EPS > TTM EPS > 0 ("forward PE below the current PE: earnings are expected to grow").
  PEGF    forward PEG = (close / NTM EPS) / (100 x (NTM / TTM - 1)) at or below 1, with TTM > 0 and growth > 0.
  PEHIST, ROIC15, DE1, FCFUP as in tt_quality.py.
  QUALF   FWDG, PEHIST, PEGF, ROIC15, DE1 and FCFUP all pass (the checklist as he states it).
  CHEAPF  FWDG, PEGF and PEHIST (the valuation half).
Screen: passers minus the universe, same universe, rebalances, periods and survival bar as tt_quality.py.
Portfolio (engine, strategies/tt_checklist.py): on the first session of each calendar quarter, buy at the
close every eligible stock that passes QUALF; extra signals ranked by 63-session return. Initial stop 20%
below the entry, not trailed. Sell at a rebalance close when the stock no longer passes or cannot be
evaluated. 1% of equity at risk per trade (a 5% position at the 20% stop), at most 20 positions, 10% cap;
otherwise the ground rules (slippage, no margin, idle cash earns nothing). SPY with dividends as the
benchmark; periods 2016-21, 2022 on, combined; the out-of-sample period runs once.
Report: trades, win rate, expectancy in R with its standard error, CAGR, CAGR minus SPY, max drawdown, Sharpe,
yearly returns vs SPY, the ten largest winners with their share of total R, expectancy without the top five.
Variants (command line), added 2026-09-30 after the first run showed the portfolio a third invested:
  --ew   fully invested in the passers: at each rebalance the cash on hand is split equally among that
         session's entries, positions capped at a third of equity; stop and exits unchanged.
  --spy  the original 5% positions with idle capital held in SPY (engine overlay), dividends reinvested.
  --control  the hindsight-free checklist of tt_quality.py (QUAL: PE below own history, trailing PEG <= 1,
         ROIC >= 15%, debt/equity < 1, FCF rising; no forward test), fully invested as in --ew. Isolates what
         the forward proxies' foresight contributes.
"""

from __future__ import annotations

import csv
import json
import sys
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import earnings as E  # noqa: E402
import nash_panels  # noqa: E402
import panel as P  # noqa: E402
from stratlib.app import open_context  # noqa: E402
from engine import OUTPUT, PERIOD_TITLES, PERIODS, Rules, run_test  # noqa: E402
from lab import load_all  # noqa: E402
from nash_rules import PERIODS as SPREAD_PERIODS, PriceTools, quarter_stats, snapshot_index, summarize  # noqa: E402
from strategies.tt_checklist import Checklist  # noqa: E402
import tt_data as T  # noqa: E402
from tt_quality import combo, load_quality_series, quality_rows  # noqa: E402

TESTS = ("FWDG", "PEGF", "QUALF", "CHEAPF")
MAX_DAYS = int(15 * 30.5)
STOP_PCT = 20.0
VARIANT = ("ew" if "--ew" in sys.argv else "spy" if "--spy" in sys.argv else "control" if "--control" in sys.argv
           else "base")
TEST_NAME = {"base": "tt_checklist_fwd", "ew": "tt_checklist_fwd_ew", "spy": "tt_checklist_fwd_spy",
             "control": "tt_checklist_qual_ew"}[VARIANT]
REPORT = {"base": "checklist_fwd", "ew": "checklist_fwd_ew", "spy": "checklist_fwd_spy", "control": "checklist_qual_ew"}[VARIANT]
PARAMS = {"stop_pct": STOP_PCT, "equal_weight": VARIANT in ("ew", "control")}
EW_RULES = Rules(risk_pct=1.0, max_positions=20, max_position_pct=100 / 3)
RULES = {"base": Rules(risk_pct=1.0, max_positions=20, max_position_pct=10.0), "ew": EW_RULES, "control": EW_RULES,
         "spy": Rules(risk_pct=1.0, max_positions=20, max_position_pct=10.0, overlay_spy=True)}[VARIANT]
TITLE = {"base": "5% positions, idle cash uninvested", "ew": "fully invested: cash split equally among the passers (cap 33%)",
         "spy": "5% positions, idle capital in SPY",
         "control": "hindsight-free checklist (trailing PEG, no forward test), fully invested equal weight"}[VARIANT]


def load_earnings(symbols) -> dict:
    ctx = open_context()
    out = {}
    try:
        for s in symbols:
            rows = ctx.store.get_response(E._key(s), timedelta(days=36500))
            if not rows:
                continue
            rows = sorted((r for r in rows if r.get("date")), key=lambda r: r["date"])
            out[s] = ([str(r["date"])[:10] for r in rows], [r.get("epsEstimated") for r in rows],
                      [r.get("epsActual") for r in rows])
    finally:
        ctx.close()
    return out


def ntm_ttm(er, day: str) -> tuple[float, float]:
    dates, est, act = er
    k = bisect_right(dates, day)
    d = date.fromisoformat(day)
    ntm = ttm = np.nan
    fut = est[k:k + 4]
    if len(fut) == 4 and all(e is not None for e in fut) and dates[k + 3] <= (d + timedelta(days=MAX_DAYS)).isoformat():
        ntm = float(sum(fut))
    past = act[max(0, k - 4):k]
    if len(past) == 4 and all(a is not None for a in past) and dates[k - 4] >= (d - timedelta(days=MAX_DAYS)).isoformat():
        ttm = float(sum(past))
    return ntm, ttm


def checklist_rows(p, tools, series, earn, classes, i: int) -> dict[str, dict]:
    day = str(p.dates[i])
    out = {}
    for j in np.flatnonzero((p.kind == "stock") & p.eligible[i]):
        sym = str(p.symbols[j])
        s = series.get(sym)
        if s is None or classes.get(sym, ("", ""))[0] == "Financial Services":
            continue
        k = snapshot_index(s, day)
        if k is None:
            continue
        m = {name: v[k] for name, v in s.m.items()}
        c = p.close[i, j]
        mcap_t = np.nan
        if m["mccy_ok"] == 1 and m["mcap"] > 0:
            q_close = tools.close_on_or_before(j, s.dates[k])
            if np.isfinite(q_close) and q_close > 0:
                mcap_t = m["mcap"] * c / q_close
        pe_now = mcap_t / m["ni_t"] if np.isfinite(mcap_t) and m["ni_t"] > 0 else np.nan
        row = {"j": int(j)}
        if np.isfinite(m["pe_med"]) and np.isfinite(mcap_t) and np.isfinite(m["ni_t"]):
            row["PEHIST"] = float(np.isfinite(pe_now) and pe_now < m["pe_med"])
        else:
            row["PEHIST"] = np.nan
        row["ROIC15"] = float(m["roic"] >= 0.15) if not np.isnan(m["roic"]) else np.nan
        row["DE1"] = float(m["de"] < 1) if not np.isnan(m["de"]) else np.nan
        row["FCFUP"] = m["fcf_up"]
        er = earn.get(sym)
        ntm, ttm = ntm_ttm(er, day) if er else (np.nan, np.nan)
        if np.isfinite(ntm) and np.isfinite(ttm) and np.isfinite(c) and c > 0:
            row["FWDG"] = float(ntm > ttm > 0)
            if ttm > 0 and ntm > ttm:
                row["PEGF"] = float((c / ntm) / (100 * (ntm / ttm - 1)) <= 1)
            else:
                row["PEGF"] = 0.0
            row["fwd_pe"] = c / ntm if ntm > 0 else np.nan
        else:
            row["FWDG"] = row["PEGF"] = np.nan
            row["fwd_pe"] = np.nan
        row["QUALF"] = combo(row, ("FWDG", "PEHIST", "PEGF", "ROIC15", "DE1", "FCFUP"))
        row["CHEAPF"] = combo(row, ("FWDG", "PEGF", "PEHIST"))
        out[sym] = row
    return out


def read_trades(period: str) -> list[dict]:
    path = OUTPUT / TEST_NAME / f"trades_{period}.csv"
    with path.open(encoding="utf-8") as f:
        return [{**r, "r": float(r["r"]), "return_pct": float(r["return_pct"]), "pnl": float(r["pnl"])}
                for r in csv.DictReader(f)]


def trade_stats(trades: list[dict]) -> dict:
    rs = np.array([t["r"] for t in trades if np.isfinite(t["r"])])
    if not len(rs):
        return {}
    se = rs.std(ddof=1) / np.sqrt(len(rs)) if len(rs) > 1 else np.nan
    order = sorted(trades, key=lambda t: -t["r"])
    total_r = rs.sum()
    top10 = [{"ticker": t["ticker"], "entry": t["entry_date"], "exit": t["exit_date"], "return_pct": t["return_pct"],
              "r": t["r"], "share_of_total_r": 100 * t["r"] / total_r if total_r > 0 else np.nan,
              "exit_reason": t["exit_reason"]} for t in order[:10]]
    rest = np.array([t["r"] for t in order[5:]])
    return {"n": int(len(rs)), "expectancy_r": float(rs.mean()), "se_r": float(se), "total_r": float(total_r),
            "top10": top10, "expectancy_without_top5": float(rest.mean()) if len(rest) else np.nan,
            "se_without_top5": float(rest.std(ddof=1) / np.sqrt(len(rest))) if len(rest) > 1 else np.nan}


def screen(panel) -> dict:
    """Passing sets per rebalance row of the main panel, plus the spread statistics; cached per data date."""
    cache = T.OUT / "checklist_fwd_screen.json"
    if cache.exists():
        saved = json.load(open(cache, encoding="utf-8"))
        if saved.get("data_through") == str(panel.dates[-1]) and "passes_qual" in saved:
            for key in ("passes", "passes_qual"):
                saved[key] = {int(k): frozenset(v) for k, v in saved[key].items()}
            return saved
    series, classes = load_quality_series()
    earn = load_earnings(series.keys())
    print(f"Company histories: {len(series):,}; earnings histories: {len(earn):,}", flush=True)
    panels = [("holdout", nash_panels.load_holdout(), "2011-01-01", "2015-12-31"), ("main", panel, "2016-01-01", "9999-12-31")]
    per_quarter = {t: [] for t in TESTS}
    passes, passes_qual, sizes, pass_counts, pass_counts_qual = {}, {}, [], [], []
    for tag, p, a, b in panels:
        tools = PriceTools(p)
        rows_i = nash_panels.rebalance_rows(p, a, b)
        all_rows = nash_panels.rebalance_rows(p, a)
        for i in rows_i:
            later = [r for r in all_rows if r > i]
            nxt = later[0] if later else len(p.dates) - 1
            chars = checklist_rows(p, tools, series, earn, classes, i)
            fwd = {s: tools.forward(r["j"], i, nxt) for s, r in chars.items()}
            day = str(p.dates[i])
            evaluable = sum(1 for r in chars.values() if not np.isnan(r["QUALF"]))
            passing = frozenset(r["j"] for r in chars.values() if r["QUALF"] == 1)
            sizes.append((day, len(chars), evaluable))
            pass_counts.append((day, len(passing)))
            if tag == "main":
                passes[i] = passing
                q_rows = quality_rows(p, tools, series, classes, i)
                passes_qual[i] = frozenset(int(r["j"]) for r in q_rows.values() if r["QUAL"] == 1)
                pass_counts_qual.append((day, len(passes_qual[i])))
            for t in TESTS:
                q = quarter_stats(list(chars.items()), fwd, t)
                if q:
                    per_quarter[t].append((day, q))
            print(f"  {day}: universe {len(chars):,}, with forward data {evaluable:,}, passing {len(passing)}", flush=True)

    spreads = {}
    survivors = []
    for t in TESTS:
        for period, (a, b) in SPREAD_PERIODS.items():
            qs = [(d, q) for d, q in per_quarter[t] if a <= d <= b]
            if len(qs) >= 3:
                spreads[f"{t} {period}"] = summarize(qs)
        r = {pp: spreads.get(f"{t} {pp}") for pp in SPREAD_PERIODS}
        ins, hold, late = r["in-sample 2016-21"], r["holdout 2011-15"], r["2022 on"]
        if ins and hold and late and ins["t"] >= 3 and hold["mean_q"] > 0 and late["mean_q"] > 0 and ins["years_pos"] >= 4:
            survivors.append(t)

    out = {"data_through": str(panel.dates[-1]), "passes": passes, "passes_qual": passes_qual, "spreads": spreads,
           "survivors": survivors, "sizes": sizes, "pass_counts": pass_counts, "pass_counts_qual": pass_counts_qual}
    json.dump({**out, "passes": {str(k): sorted(v) for k, v in passes.items()},
               "passes_qual": {str(k): sorted(v) for k, v in passes_qual.items()}}, open(cache, "w", encoding="utf-8"),
              default=float)
    return out


def main() -> None:
    panel, bench = load_all()
    sc = screen(panel)
    spreads, survivors = sc["spreads"], sc["survivors"]
    passes = sc["passes_qual"] if VARIANT == "control" else sc["passes"]
    pass_counts = sc["pass_counts_qual"] if VARIANT == "control" else sc["pass_counts"]
    Checklist.passes = passes
    summary = run_test(panel, TEST_NAME, Checklist, PARAMS, bench, rules=RULES, description=f"{TITLE}.\n\n{__doc__}")
    res = summary["results"]
    stats = {period: trade_stats(read_trades(period)) for period in PERIODS}

    lines = [f"# Test 3b: the checklist with forward-estimate proxies ({TITLE})", "", "Rules: docstring of `tt_checklist_fwd.py`. "
             "The forward figures embed hindsight (see the docstring), so this is an upper bound for the rule.", "",
             f"Data through {panel.dates[-1]}. Engine report and trade lists: `output/{TEST_NAME}/`.", "",
             "## Portfolio", ""]
    hdr = "| | " + " | ".join(PERIOD_TITLES[p] for p in PERIODS) + " |"
    lines += [hdr, "|---|" + "---|" * len(PERIODS)]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(res[p], stats[p]) for p in PERIODS) + " |")

    row("Trades", lambda m, s: f"{m['trades']:,}")
    row("Win rate", lambda m, s: f"{m['win_rate']:.1f}%")
    row("Expectancy (R) ± standard error", lambda m, s: f"{s['expectancy_r']:+.3f} ± {s['se_r']:.3f}" if s else "n/a")
    row("Expectancy without the top 5 trades", lambda m, s: f"{s['expectancy_without_top5']:+.3f} ± {s['se_without_top5']:.3f}" if s else "n/a")
    row("Average win / loss", lambda m, s: f"{m['avg_win_pct']:+.1f}% / {m['avg_loss_pct']:+.1f}%")
    row("CAGR", lambda m, s: f"{m['cagr']:.1f}%")
    row("SPY CAGR with dividends", lambda m, s: f"{m['spy']['cagr']:.1f}%")
    row("CAGR minus SPY", lambda m, s: f"{m['cagr'] - m['spy']['cagr']:+.1f} pts")
    row("Max drawdown", lambda m, s: f"{m['max_drawdown']:.1f}% (SPY {m['spy']['max_drawdown']:.1f}%)")
    row("Sharpe", lambda m, s: f"{m['sharpe']:.2f} (SPY {m['spy']['sharpe']:.2f})")
    row("Average holding (days)", lambda m, s: f"{m['avg_holding_days']:.0f}")
    row("Average invested", lambda m, s: f"{m['exposure_avg_invested_pct']:.0f}%")
    row("Exit reasons", lambda m, s: ", ".join(f"{k} {v}" for k, v in sorted(m["exit_reasons"].items(), key=lambda x: -x[1])))
    comb = res["combined"]
    lines += ["", "## Yearly returns vs SPY (combined run)", "", "| year | portfolio | SPY with dividends | difference |", "|---|---|---|---|"]
    for y in comb["yearly"]:
        a, b = comb["yearly"][y], comb["spy"]["yearly"][y]
        lines.append(f"| {y} | {a:+.1f}% | {b:+.1f}% | {a - b:+.1f} |")
    s = stats["combined"]
    lines += ["", f"## Ten largest winners by R (combined run; total R over {s['n']} trades = {s['total_r']:+.1f})", "",
              "| ticker | entry | exit | return | R | share of total R | exit |", "|---|---|---|---|---|---|---|"]
    for t in s["top10"]:
        lines.append(f"| {t['ticker']} | {t['entry']} | {t['exit']} | {t['return_pct']:+.1f}% | {t['r']:+.2f} | "
                     f"{t['share_of_total_r']:.0f}% | {t['exit_reason']} |")
    lines += ["", "## Screen: passers minus universe, annualized points (t-stat, % quarters positive, years positive, universe with forward data, share passing)", ""]
    for period in SPREAD_PERIODS:
        lines += [f"### {period}", "", "| test | pts/yr | t | quarters + | years + | universe | passing |", "|---|---|---|---|---|---|---|"]
        for t in TESTS:
            r = spreads.get(f"{t} {period}")
            if r:
                lines.append(f"| {t} | {r['annual']:+.1f} | {r['t']:+.1f} | {r['hit']:.0f}% | {r['years_pos']}/{len(r['years'])} "
                             f"| {r['avg_n']:.0f} | {r.get('share', float('nan')):.0f}% |")
        lines.append("")
    lines.append(f"Screen survivors: {survivors or 'none'}")
    lines += ["", "Passing stocks per rebalance (main panel): " + ", ".join(f"{d[:7]} {n}" for d, n in pass_counts if d >= "2016")]
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / f"{REPORT}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump({"rules": __doc__, "variant": VARIANT, "engine": res, "trade_stats": stats, "spreads": spreads,
               "survivors": survivors, "pass_counts": pass_counts}, open(T.OUT / f"{REPORT}.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
