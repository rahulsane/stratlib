"""The Traveling Trader's fundamentals checklist as a cross-sectional screen. Rules fixed before results.

Same universe, rebalance dates, forward returns, periods and survival bar as the Nash rule study:
non-financial stocks passing the liquidity test ($5 as-traded close, $20M of 20-day average dollar volume) on
the first session of each calendar quarter, with 8 consecutive quarters of statements available before the day (filing date) and no
older than 200 days; forward return = rebalance close to the next rebalance close, price only, equal-weighted;
statistic per quarter = mean return of the passers minus the universe mean, in percentage points.

Tests (his checklist, point-in-time; forward PE and PEG are replaced by trailing versions because the data provider has no
historical analyst estimates):
  PEHIST  trailing PE (market cap moved to the rebalance day by the price change, over TTM net income, both
          positive) below the median of the company's trailing PE at the previous 12 quarter-ends (at least 8
          available). Negative TTM earnings fail.
  PEGT    trailing PEG = trailing PE / (100 x TTM net-income growth on a year earlier) at or below 1, with
          positive earnings and growth; fails otherwise when the current and prior-year figures exist.
  ROIC15  ROIC >= 15% at the latest quarter (the Nash-study definition).
  DE1     total debt / equity below 1, with positive equity.
  FCFUP   TTM free cash flow above its level 4 quarters earlier, which is above its level 8 quarters earlier.
  RECORD  TTM revenue at a 12-quarter high and TTM free cash flow positive.
  QUAL    PEHIST, PEGT, ROIC15, DE1 and FCFUP all pass (the checklist).
  QUAL4   ROIC15, DE1, FCFUP and RECORD (quality without the valuation tests).
  CHEAP   PEHIST and PEGT (valuation only).
A test survives only if its in-sample (2016-21) t >= 3, its mean is positive in the holdout (2011-15) and in
2022+, and it is positive in at least 4 of the 6 in-sample years.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nash_panels  # noqa: E402
import panel as P  # noqa: E402
import report_rules as rr  # noqa: E402
from stratlib.app import open_context  # noqa: E402
from nash_fundamentals import classifications  # noqa: E402
from nash_rules import PERIODS, PriceTools, _d, _num, quarter_stats, snapshot_index, summarize  # noqa: E402
from nash_rules_data import KEY  # noqa: E402
from nash_screen import available_from  # noqa: E402
import tt_data as T  # noqa: E402

TESTS = ("PEHIST", "PEGT", "ROIC15", "DE1", "FCFUP", "RECORD", "QUAL", "QUAL4", "CHEAP")


@dataclass
class QSeries:
    dates: list
    avail: list
    ccy: list
    run: np.ndarray
    m: dict


def quality_series(doc: dict) -> QSeries | None:
    inc = sorted((r for r in doc.get("income", []) if r.get("date")), key=lambda r: r["date"])
    if len(inc) < 8:
        return None
    by = {k: {r["date"]: r for r in doc.get(k, []) if r.get("date")} for k in ("balance", "cash", "metrics")}
    n = len(inc)
    dates = [r["date"] for r in inc]
    ccy = [r.get("reportedCurrency") for r in inc]
    run = np.ones(n, dtype=int)
    for k in range(1, n):
        gap = (_d(dates[k]) - _d(dates[k - 1])).days
        if 60 <= gap <= 120 and ccy[k] == ccy[k - 1]:
            run[k] = run[k - 1] + 1

    def col(src, key):
        if src == "income":
            return np.array([_num(r.get(key)) for r in inc])
        return np.array([_num((by[src].get(d) or {}).get(key)) for d in dates])

    rev, oi, pretax, tax, ni = (col("income", k) for k in
                                ("revenue", "operatingIncome", "incomeBeforeTax", "incomeTaxExpense", "netIncome"))
    fcf = col("cash", "freeCashFlow")
    cash, debt, equity = (col("balance", k) for k in ("cashAndShortTermInvestments", "totalDebt", "totalStockholdersEquity"))
    mcap = col("metrics", "marketCap")
    mccy = [(by["metrics"].get(d) or {}).get("reportedCurrency") for d in dates]

    def ttm(x):
        out = np.full(n, np.nan)
        for k in range(3, n):
            if run[k] >= 4:
                out[k] = x[k - 3:k + 1].sum()
        return out

    def lag(x, q):
        out = np.full(n, np.nan)
        out[q:] = x[:-q]
        return out

    rev_t, oi_t, pre_t, tax_t, ni_t, fcf_t = (ttm(x) for x in (rev, oi, pretax, tax, ni, fcf))
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(pre_t > 0, np.clip(tax_t / np.where(pre_t > 0, pre_t, 1), 0, 0.35), 0.21)
        rate = np.where(np.isfinite(rate), rate, 0.21)
        nopat = oi_t * (1 - rate)
        ic = equity + debt - cash
        roic = np.where(ic > 0, nopat / np.where(ic > 0, ic, 1), np.where(nopat > 0, np.inf, -np.inf))
        roic = np.where(np.isfinite(nopat) & np.isfinite(ic), roic, np.nan)
        de = np.where(equity > 0, debt / np.where(equity > 0, equity, 1), np.inf)
        de = np.where(np.isfinite(debt) & np.isfinite(equity), de, np.nan)
        mc_ok = np.array([a == b for a, b in zip(mccy, ccy)])
        pe_q = np.where((mcap > 0) & (ni_t > 0) & mc_ok, mcap / np.where(ni_t > 0, ni_t, 1), np.nan)
        pe_med = np.full(n, np.nan)
        for k in range(12, n):
            if run[k] >= 12:
                w = pe_q[k - 12:k]
                w = w[np.isfinite(w)]
                if len(w) >= 8:
                    pe_med[k] = np.median(w)
        ni_lag = lag(ni_t, 4)
        ni_g = np.where((run >= 8) & (ni_lag > 0), ni_t / np.where(ni_lag > 0, ni_lag, 1) - 1, np.nan)
        ni_lag_ok = (run >= 8) & np.isfinite(ni_lag) & np.isfinite(ni_t)
        fcf_up = np.full(n, np.nan)
        for k in range(8, n):
            if run[k] >= 12 and np.isfinite(fcf_t[k]) and np.isfinite(fcf_t[k - 4]) and np.isfinite(fcf_t[k - 8]):
                fcf_up[k] = float(fcf_t[k] > fcf_t[k - 4] > fcf_t[k - 8])
        record = np.full(n, np.nan)
        for k in range(11, n):
            if run[k] >= 12 and np.isfinite(rev_t[k - 11:k + 1]).all() and np.isfinite(fcf_t[k]):
                record[k] = float(rev_t[k] >= rev_t[k - 11:k + 1].max() and fcf_t[k] > 0)
    m = {"ni_t": ni_t, "roic": roic, "de": de, "pe_med": pe_med, "ni_g": ni_g, "ni_lag_ok": ni_lag_ok.astype(float),
         "fcf_up": fcf_up, "record": record, "mcap": mcap, "mccy_ok": mc_ok.astype(float)}
    return QSeries(dates=dates, avail=[available_from(r) for r in inc], ccy=ccy, run=run, m=m)


def load_quality_series() -> tuple[dict, dict]:
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        series = {}
        for key in ctx.store.document_keys(KEY):
            doc = ctx.store.document(key)
            if doc and "error" not in doc:
                s = quality_series(doc)
                if s:
                    series[key[len(KEY):]] = s
    finally:
        ctx.close()
    return series, classes


def combo(row: dict, names: tuple) -> float:
    vals = [row[nm] for nm in names]
    if any(v == 0 for v in vals if not np.isnan(v)):
        return 0.0
    if any(np.isnan(v) for v in vals):
        return np.nan
    return 1.0


def quality_rows(p, tools, series, classes, i: int) -> dict[str, dict]:
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
        row = {"j": j}
        if np.isfinite(m["pe_med"]) and np.isfinite(mcap_t) and np.isfinite(m["ni_t"]):
            row["PEHIST"] = float(np.isfinite(pe_now) and pe_now < m["pe_med"])
        else:
            row["PEHIST"] = np.nan
        if np.isfinite(mcap_t) and m["ni_lag_ok"] == 1:
            row["PEGT"] = float(np.isfinite(pe_now) and np.isfinite(m["ni_g"]) and m["ni_g"] > 0
                                and pe_now / (100 * m["ni_g"]) <= 1)
        else:
            row["PEGT"] = np.nan
        row["ROIC15"] = float(m["roic"] >= 0.15) if not np.isnan(m["roic"]) else np.nan
        row["DE1"] = float(m["de"] < 1) if not np.isnan(m["de"]) else np.nan
        row["FCFUP"] = m["fcf_up"]
        row["RECORD"] = m["record"]
        row["QUAL"] = combo(row, ("PEHIST", "PEGT", "ROIC15", "DE1", "FCFUP"))
        row["QUAL4"] = combo(row, ("ROIC15", "DE1", "FCFUP", "RECORD"))
        row["CHEAP"] = combo(row, ("PEHIST", "PEGT"))
        out[sym] = row
    return out


def rules_section() -> list[str]:
    return rr.section(
        ["This test asks whether the Traveling Trader's fundamentals checklist picks stocks that beat the average "
         "stock. Each quarter it screens the whole universe and compares the next quarter's return of the stocks that "
         "pass with that of all of them. The rules were fixed before any results were seen."],
        rr.CHECKLIST_SOURCE,
        rr.CHECKLIST_UNIVERSE,
        ["Tests, each checked point in time. The data has no history of analyst estimates, so his forward PE and "
         "PEG are replaced by trailing versions.", "", *rr.CHECKLIST_TRAILING[2:],
         "- RECORD: TTM revenue at a 12-quarter high and TTM free cash flow positive.",
         "- QUAL: PEHIST, PEGT, ROIC15, DE1 and FCFUP all pass. This is the checklist.",
         "- QUAL4: ROIC15, DE1, FCFUP and RECORD, quality without the valuation tests.",
         "- CHEAP: PEHIST and PEGT, valuation only."],
        ["How a test is scored:", "",
         "- Return: from one rebalance close to the next, split-adjusted price only, equal-weighted. A stock whose "
         "prices end during the quarter returns to its last close.",
         "- Each quarter's statistic is the passers' mean return minus the universe's mean, in percentage points. "
         "*pts/yr* is the quarterly average times 4; *t* is its t-statistic across quarters; *quarters +* and *years "
         "+* count how often it was positive.",
         "- Periods: a holdout of 2011–2015 on a separate price panel, in-sample 2016–2021, and 2022 on.",
         "- A test survives only if its in-sample t is at least 3, its mean is positive in the holdout and from 2022, "
         "and it is positive in at least 4 of the 6 in-sample years.",
         "- There are no trading costs; this is a screen, not a portfolio. Statements are as restated, not as first "
         "reported, and delisted stocks are thin before 2021."],
    )


def main() -> None:
    series, classes = load_quality_series()
    print(f"Company histories: {len(series):,}", flush=True)
    panels = [(nash_panels.load_holdout(), "2011-01-01", "2015-12-31"), (P.load(), "2016-01-01", "9999-12-31")]
    per_quarter = {t: [] for t in TESTS}
    sizes = []
    for p, a, b in panels:
        tools = PriceTools(p)
        rows_i = nash_panels.rebalance_rows(p, a, b)
        all_rows = nash_panels.rebalance_rows(p, a)
        for i in rows_i:
            later = [r for r in all_rows if r > i]
            nxt = later[0] if later else len(p.dates) - 1
            chars = quality_rows(p, tools, series, classes, i)
            fwd = {s: tools.forward(r["j"], i, nxt) for s, r in chars.items()}
            day = str(p.dates[i])
            sizes.append((day, len(chars)))
            for t in TESTS:
                q = quarter_stats(list(chars.items()), fwd, t)
                if q:
                    per_quarter[t].append((day, q))
            print(f"  {day}: universe {len(chars):,}", flush=True)

    results = {"rules": __doc__, "universe_sizes": sizes, "tests": {}}
    survivors = []
    for t in TESTS:
        for period, (a, b) in PERIODS.items():
            qs = [(d, q) for d, q in per_quarter[t] if a <= d <= b]
            if len(qs) >= 3:
                results["tests"][f"{t} {period}"] = summarize(qs)
        r = {pp: results["tests"].get(f"{t} {pp}") for pp in PERIODS}
        ins, hold, late = r["in-sample 2016-21"], r["holdout 2011-15"], r["2022 on"]
        ok = bool(ins and hold and late and ins["t"] >= 3 and hold["mean_q"] > 0 and late["mean_q"] > 0 and ins["years_pos"] >= 4)
        results["tests"][f"verdict {t}"] = ok
        if ok:
            survivors.append(t)
    results["survivors"] = survivors

    lines = ["# The fundamentals checklist as a screen", "", "Passers minus universe, annualized points "
             "(quarterly mean x 4), t-stat, % of quarters positive, years positive, average universe size, share of "
             "the universe passing.", "", *rules_section()]
    for period in PERIODS:
        lines += [f"## {period}", "", "| test | pts/yr | t | quarters + | years + | universe | passing |", "|---|---|---|---|---|---|---|"]
        for t in TESTS:
            r = results["tests"].get(f"{t} {period}")
            if r:
                lines.append(f"| {t} | {r['annual']:+.1f} | {r['t']:+.1f} | {r['hit']:.0f}% | {r['years_pos']}/{len(r['years'])} "
                             f"| {r['avg_n']:.0f} | {r.get('share', float('nan')):.0f}% |")
        lines.append("")
    lines += [f"Survivors (in-sample t >= 3, positive in the holdout and 2022+, 4 of 6 in-sample years): {survivors or 'none'}", ""]
    T.OUT.mkdir(parents=True, exist_ok=True)
    (T.OUT / "quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.dump(results, open(T.OUT / "quality.json", "w"), indent=1, default=float)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
