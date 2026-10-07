"""Rule study for the Nash screen: does each candidate rule predict returns across the whole universe?

Fixed before any of these statistics were computed.

Universe at each rebalance (first session of each calendar quarter): stocks that pass the liquidity test
that day ($5 as-traded close, $20M of 20-day average dollar volume), outside Financial Services, with 8 consecutive quarters of statements in one
currency available before the day (filing date; period end + 45 days where the reported filing date is the
period end) and no older than 200 days.

Forward return: rebalance close to the next rebalance close, split-adjusted price only; a stock whose prices
end in the quarter returns to its last close. Equal-weighted means, no winsorizing (a version winsorized at
the 1st/99th percentile each quarter is reported as a check).

Tests (statistic per quarter, in percentage points; positive = the rule helps):
  MOM    12-month return skipping the last month (sessions t-252..t-21); top fifth minus universe
  FCFY   TTM free cash flow / market cap; universe minus the lowest-yield fifth
  EVS    enterprise value / TTM revenue; universe minus the highest fifth
         (market cap and EV from the data provider's key metrics at the quarter end, moved to the rebalance day with the
         price change; skipped where the key metrics and the statements use different currencies)
  ROIC3  ROIC >= 15% at the latest quarter and 4 and 8 quarters earlier; passers minus universe.
         ROIC = TTM operating income x (1 - tax rate) / (equity + total debt - cash and short-term
         investments). Tax rate = TTM tax / TTM pre-tax income, clipped to 0-35%, 21% if pre-tax <= 0.
         Invested capital <= 0 passes if after-tax operating income is positive.
  DIL    diluted share count up less than 3% on a year earlier; passers minus universe
  CONS   revenue up >= 10% on a year earlier in each of the last 8 quarters, and TTM operating margin
         not below its level 4 quarters earlier; passers minus universe
  TREND  close above its 200-session average; passers minus universe
  DIP    close 20% or more below the 252-session high (Nash's add-more signal); passers minus universe

Periods: holdout 2011-2015 (holdout panel), in-sample 2016-2021, 2022 on (already partly seen, a check only).

A rule survives only if, in the broad universe:
  in-sample t >= 3.0, the mean is positive in the holdout and in 2022+, and it is positive in at least 4 of
  the 6 in-sample calendar years.
Secondary (reported, not used for selection): the same statistics inside the base Nash set below, with
halves instead of fifths.

Final portfolio, run once after this study:
  Base filter: Nash rules 1-4 with FCF margin >= 15%, one currency, FCF margin within +-100%, prior-year
  revenue >= $1B, financials excluded, no rule 7.
  Surviving binary rules become extra filters; surviving continuous rules join a composite rank (mean
  percentile within the base set of revenue growth, FCF margin and each continuous survivor).
  Holdings are sold after failing the filters at two consecutive rebalances; empty slots are filled by the
  composite rank, at most 30% of slots per sector (3 of 10, 9 of 30). Forms: 50% SPY + 5% x 10 ("blend"), 10% x 10, 3.3% x 30; empty
  slots go to SPY. Also run with no surviving rules ("mechanics only") to separate the two effects.
  Benchmarks: SPY with dividends, and the equal-weighted universe (price only).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import nash_panels
import panel as P
from stratlib.app import open_context
from nash_fundamentals import classifications
from nash_rules_data import KEY
from nash_screen import available_from
from nash_screen_clean import FX

OUT = Path(__file__).resolve().parent / "output" / "nash_rules"
STALE_DAYS = 200
PERIODS = {"holdout 2011-15": ("2011-01-01", "2015-12-31"), "in-sample 2016-21": ("2016-01-01", "2021-12-31"),
           "2022 on": ("2022-01-01", "9999-12-31")}
TESTS = ("MOM", "FCFY", "EVS", "ROIC3", "DIL", "CONS", "TREND", "DIP")
CONTINUOUS = {"MOM": "high", "FCFY": "high", "EVS": "low"}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else np.nan


@dataclass
class Series:
    """Quarterly history of one company, oldest first, with TTM measures per quarter."""
    dates: list
    avail: list
    ccy: list
    run: np.ndarray
    m: dict  # name -> array aligned with dates


def build_series(doc: dict) -> Series | None:
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

    rev, opx, oi = col("income", "revenue"), col("income", "operatingExpenses"), col("income", "operatingIncome")
    pretax, tax, shares = col("income", "incomeBeforeTax"), col("income", "incomeTaxExpense"), col("income", "weightedAverageShsOutDil")
    fcf = col("cash", "freeCashFlow")
    cash, debt, leases, equity = (col("balance", k) for k in ("cashAndShortTermInvestments", "totalDebt",
                                                               "capitalLeaseObligations", "totalStockholdersEquity"))
    mcap, ev = col("metrics", "marketCap"), col("metrics", "enterpriseValue")
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

    rev_t, opx_t, oi_t, fcf_t, pre_t, tax_t = (ttm(x) for x in (rev, opx, oi, fcf, pretax, tax))
    ok8 = run >= 8
    with np.errstate(invalid="ignore", divide="ignore"):
        rev_g = np.where(ok8 & (lag(rev_t, 4) > 0) & (rev_t > 0), rev_t / lag(rev_t, 4) - 1, np.nan)
        opx_g = np.where(ok8 & (lag(opx_t, 4) > 0) & (opx_t > 0), opx_t / lag(opx_t, 4) - 1, np.nan)
        om = np.where(rev_t > 0, oi_t / rev_t, np.nan)
        fcfm = np.where(rev_t > 0, fcf_t / rev_t, np.nan)
        lease = np.nan_to_num(leases)
        debt_ex = np.where((lease > 0) & (lease <= debt), debt - lease, debt)
        cash_gt = np.where(np.isfinite(cash) & np.isfinite(debt_ex), (cash > debt_ex).astype(float), np.nan)
        rate = np.where(pre_t > 0, np.clip(tax_t / np.where(pre_t > 0, pre_t, 1), 0, 0.35), 0.21)
        rate = np.where(np.isfinite(rate), rate, 0.21)
        nopat = oi_t * (1 - rate)
        ic = equity + debt - cash
        roic = np.where(ic > 0, nopat / np.where(ic > 0, ic, 1), np.where(nopat > 0, np.inf, -np.inf))
        roic = np.where(np.isfinite(nopat) & np.isfinite(ic), roic, np.nan)
        roic3 = np.full(n, np.nan)
        for k in range(8, n):
            if run[k] >= 12 and all(np.isfinite(roic[k - q]) or np.isinf(roic[k - q]) for q in (0, 4, 8)):
                roic3[k] = float(all(roic[k - q] >= 0.15 for q in (0, 4, 8)))
        dil = np.where((run >= 5) & (lag(shares, 4) > 0) & (shares > 0), shares / lag(shares, 4) - 1, np.nan)
        q_yoy = np.where((run >= 5) & (lag(rev, 4) > 0), rev / lag(rev, 4) - 1, np.nan)
        cons = np.full(n, np.nan)
        for k in range(11, n):
            if run[k] >= 12:
                window = q_yoy[k - 7:k + 1]
                if np.isfinite(window).all() and np.isfinite(om[k]) and np.isfinite(om[k - 4]):
                    cons[k] = float((window >= 0.10).all() and om[k] >= om[k - 4])
        rev0_usd = lag(rev_t, 4) / np.array([FX.get(c, np.nan) for c in ccy])
    m = {"rev_g": rev_g, "opx_g": opx_g, "om": om, "fcfm": fcfm, "cash_gt": cash_gt, "roic3": roic3, "dil": dil,
         "cons": cons, "rev0_usd": rev0_usd, "rev_t": rev_t, "fcf_t": fcf_t, "mcap": mcap, "ev": ev,
         "mccy_ok": np.array([float(a == b) for a, b in zip(mccy, ccy)])}
    return Series(dates=dates, avail=[available_from(r) for r in inc], ccy=ccy, run=run, m=m)


def snapshot_index(s: Series, day: str) -> int | None:
    best = None
    for k, a in enumerate(s.avail):
        if a < day:
            best = k if best is None or s.dates[k] > s.dates[best] else best
    if best is None or s.run[best] < 8 or (_d(day) - _d(s.dates[best])).days > STALE_DAYS:
        return None
    return best


def nash_base(s: Series, k: int) -> bool:
    """Rules 1-4 (FCF margin), one currency, FCF margin within +-100%, prior-year revenue >= $1B."""
    m = {name: v[k] for name, v in s.m.items()}
    return bool(m["cash_gt"] == 1 and m["rev_g"] >= 0.10 and np.isfinite(m["fcfm"]) and 0.15 <= m["fcfm"] <= 1
                and np.isfinite(m["opx_g"]) and m["rev_g"] > m["opx_g"] and m["rev0_usd"] >= 1e9)


# ----------------------------------------------------------------------------------------------

class PriceTools:
    def __init__(self, p):
        self.p = p
        self.sma200 = np.full_like(p.close_ff, np.nan)
        c = p.close_ff
        csum = np.nancumsum(np.nan_to_num(c), axis=0)
        cnt = np.cumsum(np.isfinite(c), axis=0)
        for i in range(199, len(p.dates)):
            lo = i - 200
            tot = csum[i] - (csum[lo] if lo >= 0 else 0)
            num = cnt[i] - (cnt[lo] if lo >= 0 else 0)
            self.sma200[i] = np.where(num >= 190, tot / np.maximum(num, 1), np.nan)
        import pandas as pd
        self.high252 = pd.DataFrame(c).rolling(252, min_periods=240).max().to_numpy()

    def close_on_or_before(self, j: int, day: str) -> float:
        i = int(np.searchsorted(self.p.dates, day, side="right")) - 1
        while i >= 0 and not np.isfinite(self.p.close_ff[i, j]):
            i -= 1
        return self.p.close_ff[i, j] if i >= 0 else np.nan

    def forward(self, j: int, i: int, i2: int) -> float:
        p = self.p
        start = p.close[i, j]
        end_i = min(i2, p.last_bar[j])
        while end_i > i and not np.isfinite(p.close[end_i, j]):
            end_i -= 1
        return p.close[end_i, j] / start - 1 if end_i > i else 0.0


def characteristics(p, tools, series, classes, i: int) -> dict[str, dict]:
    """Per eligible stock: forward-return inputs and every test's value at rebalance row i."""
    day = str(p.dates[i])
    out = {}
    for j in np.flatnonzero((p.kind == "stock") & p.eligible[i]):
        sym = str(p.symbols[j])
        s = series.get(sym)
        if s is None or classes.get(sym, ("", ""))[0] == "Financial Services":
            continue
        k = snapshot_index(s, day)
        if k is None or not np.isfinite(s.m["rev_g"][k]):
            continue
        m = {name: v[k] for name, v in s.m.items()}
        c = p.close[i, j]
        row = {"j": j, "k": k, "sector": classes.get(sym, ("", ""))[0], "base": nash_base(s, k),
               "rev_g": m["rev_g"], "fcfm": m["fcfm"]}
        past, skip = (p.close_ff[i - 252, j], p.close_ff[i - 21, j]) if i >= 252 else (np.nan, np.nan)
        row["MOM"] = skip / past - 1 if np.isfinite(past) and past > 0 and np.isfinite(skip) else np.nan
        mcap_t = np.nan
        if m["mccy_ok"] == 1 and m["mcap"] > 0:
            q_close = tools.close_on_or_before(j, s.dates[k])
            if np.isfinite(q_close) and q_close > 0:
                mcap_t = m["mcap"] * c / q_close
        row["FCFY"] = m["fcf_t"] / mcap_t if np.isfinite(mcap_t) and np.isfinite(m["fcf_t"]) else np.nan
        ev_t = mcap_t + (m["ev"] - m["mcap"]) if np.isfinite(mcap_t) and np.isfinite(m["ev"]) else np.nan
        row["EVS"] = ev_t / m["rev_t"] if np.isfinite(ev_t) and m["rev_t"] > 0 else np.nan
        row["ROIC3"] = m["roic3"]
        row["DIL"] = float(m["dil"] < 0.03) if np.isfinite(m["dil"]) else np.nan
        row["CONS"] = m["cons"]
        sma = tools.sma200[i, j]
        row["TREND"] = float(c > sma) if np.isfinite(sma) else np.nan
        hi = tools.high252[i, j]
        row["DIP"] = float(c <= 0.8 * hi) if np.isfinite(hi) else np.nan
        out[sym] = row
    return out


def quarter_stats(rows: list[dict], fwd: dict, test: str, halves: bool = False) -> dict | None:
    """Spread for one quarter. Universe = stocks with a value for the test."""
    vals = [(r[test], fwd[s]) for s, r in rows if np.isfinite(r[test])]
    if len(vals) < (10 if halves else 25):
        return None
    x = np.array([v for v, _ in vals])
    y = np.array([f for _, f in vals])
    lo, hi = np.percentile(y, [1, 99])
    yw = np.clip(y, lo, hi)
    u, uw = y.mean(), yw.mean()
    if test in CONTINUOUS:
        cuts = np.percentile(x, [50] if halves else [20, 40, 60, 80])
        bucket = np.searchsorted(cuts, x, side="right")          # 0 = lowest value
        n_b = 2 if halves else 5
        means = [y[bucket == b].mean() - u if (bucket == b).any() else np.nan for b in range(n_b)]
        means_w = [yw[bucket == b].mean() - uw if (bucket == b).any() else np.nan for b in range(n_b)]
        if CONTINUOUS[test] == "high":
            spread, spread_w = means[-1], means_w[-1]
        else:
            spread, spread_w = -means[-1], -means_w[-1]
        if test == "FCFY":                                        # universe minus the most expensive group
            spread, spread_w = -means[0], -means_w[0]
        return {"spread": spread, "spread_w": spread_w, "buckets": means, "n": len(x)}
    passed = x == 1
    if passed.sum() < 5 or (~passed).sum() < 5:
        return None
    return {"spread": y[passed].mean() - u, "spread_w": yw[passed].mean() - uw, "share": passed.mean(), "n": len(x)}


def summarize(qs: list[tuple[str, dict]]) -> dict:
    s = np.array([q["spread"] for _, q in qs]) * 100
    w = np.array([q["spread_w"] for _, q in qs]) * 100
    years = {}
    for d, q in qs:
        years[d[:4]] = years.get(d[:4], 0) + 100 * q["spread"]
    t = s.mean() / (s.std(ddof=1) / math.sqrt(len(s))) if len(s) > 2 and s.std(ddof=1) > 0 else np.nan
    tw = w.mean() / (w.std(ddof=1) / math.sqrt(len(w))) if len(w) > 2 and w.std(ddof=1) > 0 else np.nan
    out = {"quarters": len(s), "mean_q": s.mean(), "annual": 4 * s.mean(), "t": t, "t_winsor": tw,
           "hit": 100 * (s > 0).mean(), "years_pos": sum(v > 0 for v in years.values()), "years": years,
           "avg_n": float(np.mean([q["n"] for _, q in qs]))}
    if "buckets" in qs[0][1]:
        out["buckets"] = list(100 * np.nanmean([q["buckets"] for _, q in qs], axis=0))
    if "share" in qs[0][1]:
        out["share"] = 100 * float(np.mean([q["share"] for _, q in qs]))
    return out


def load_series() -> tuple[dict, dict]:
    ctx = open_context()
    try:
        classes = classifications(ctx.settings.data.db_path)
        series = {}
        for key in ctx.store.document_keys(KEY):
            doc = ctx.store.document(key)
            if doc and "error" not in doc:
                s = build_series(doc)
                if s:
                    series[key[len(KEY):]] = s
    finally:
        ctx.close()
    return series, classes


def main() -> None:
    series, classes = load_series()
    print(f"Company histories: {len(series):,}")
    panels = [(nash_panels.load_holdout(), "2011-01-01", "2015-12-31"), (P.load(), "2016-01-01", "9999-12-31")]
    per_quarter = {"broad": {t: [] for t in TESTS}, "base": {t: [] for t in TESTS}}
    universe_sizes, base_sizes = [], []
    for p, a, b in panels:
        tools = PriceTools(p)
        rows_i = nash_panels.rebalance_rows(p, a, b)
        all_rows = nash_panels.rebalance_rows(p, a)             # includes the first quarter after the period
        for i in rows_i:
            later = [r for r in all_rows if r > i]
            nxt = later[0] if later else len(p.dates) - 1
            chars = characteristics(p, tools, series, classes, i)
            fwd = {s: tools.forward(r["j"], i, nxt) for s, r in chars.items()}
            day = str(p.dates[i])
            universe_sizes.append((day, len(chars)))
            base = [(s, r) for s, r in chars.items() if r["base"]]
            base_sizes.append((day, len(base)))
            for t in TESTS:
                q = quarter_stats(list(chars.items()), fwd, t)
                if q:
                    per_quarter["broad"][t].append((day, q))
                q = quarter_stats(base, fwd, t, halves=True)
                if q:
                    per_quarter["base"][t].append((day, q))
            print(f"  {day}: universe {len(chars):,}, base set {len(base)}", flush=True)

    results = {"rules": __doc__, "universe_sizes": universe_sizes, "base_sizes": base_sizes, "tests": {}}
    for scope in ("broad", "base"):
        for t in TESTS:
            for period, (a, b) in PERIODS.items():
                qs = [(d, q) for d, q in per_quarter[scope][t] if a <= d <= b]
                if len(qs) >= 3:
                    results["tests"][f"{scope} {t} {period}"] = summarize(qs)
    survivors = []
    for t in TESTS:
        r = {p: results["tests"].get(f"broad {t} {p}") for p in PERIODS}
        ins, hold, late = r["in-sample 2016-21"], r["holdout 2011-15"], r["2022 on"]
        ok = bool(ins and hold and late and ins["t"] >= 3 and hold["mean_q"] > 0 and late["mean_q"] > 0
                  and ins["years_pos"] >= 4)
        results["tests"][f"verdict {t}"] = ok
        if ok:
            survivors.append(t)
    results["survivors"] = survivors
    OUT.mkdir(parents=True, exist_ok=True)
    json.dump(results, open(OUT / "scan.json", "w"), indent=1, default=float)

    for scope in ("broad", "base"):
        print(f"\n{scope.upper()}: spread vs {'universe' if scope == 'broad' else 'base set'}, "
              f"annualized points (quarterly mean x 4), t-stat, % quarters positive, years positive")
        print(f"{'test':7}" + "".join(f"{p:>34}" for p in PERIODS) + "   winsor t (IS)")
        for t in TESTS:
            line = f"{t:7}"
            for p in PERIODS:
                r = results["tests"].get(f"{scope} {t} {p}")
                line += (f"{r['annual']:+8.1f} t{r['t']:+5.1f} {r['hit']:4.0f}% {r['years_pos']}/{len(r['years'])}y n{r['avg_n']:5.0f}"
                         if r else f"{'n/a':>34}")
            r = results["tests"].get(f"{scope} {t} in-sample 2016-21")
            line += f"   {r['t_winsor']:+5.1f}" if r else ""
            print(line)
        for t in CONTINUOUS:
            for p in PERIODS:
                r = results["tests"].get(f"{scope} {t} {p}")
                if r and "buckets" in r:
                    print(f"  {t} {p}: groups low..high vs universe, pts/quarter: "
                          + " ".join(f"{v:+.2f}" for v in r["buckets"]))
    print("\nSurvivors:", survivors or "none")


if __name__ == "__main__":
    main()
