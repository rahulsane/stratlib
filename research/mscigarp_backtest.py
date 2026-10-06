"""MSCI USA Quality GARP Select (iShares GARP): (1) self-managed, (2) as if the ETF had existed all along.

Same questions and metrics as garp_backtest.py (the S&P 500 GARP study), with one difference in sources: MSCI
publishes the index's official daily levels back to 2 December 2002, so the strategy's return comes from MSCI's own
series rather than from a rebuild. The rebuild (mscigarp_index.py, "proxy" variant) is used for what only holdings
can tell: the costs of running it yourself (turnover, slippage, idle cash, whole shares) and the taxes.

Version 2, ETF: MSCI's gross total return (the fund's benchmark) less 0.20% a year, plus 0.10% slippage on the
purchase and on the final sale. 0.20% is GARP's real all-in cost: a 0.15% fee, and its NAV trailed the index by
0.16% in 2025 and 0.22% in 2024 (the fund's market price trailed by 0.26% a year from the 3 June 2024 index switch
to September 2026).

Version 1, self-managed: copy the index's holdings at each quarterly review (iShares publishes them daily; MSCI's
growth score uses proprietary analyst forecasts, so recomputing it yourself isn't possible). Simulated on the
rebuild with the S&P study's rules: $100,000, whole shares, 0.10% slippage per side (0.25% under $20), dividends and
proceeds of deleted stocks idle until the next review. The simulation's cumulative shortfall against the rebuilt
index (costs, idle cash, rounding) is then applied, day by day, to MSCI's official series.

Taxes: as in the S&P study (15% long-term and qualified dividends, 24% short-term, paid at the first review after
April). Computed on the rebuild for both ways of owning it, so they compare like with like.

Run: PYTHONPATH=src .venv/Scripts/python research/mscigarp_backtest.py
"""

from __future__ import annotations

import csv
import json
import math
import warnings
from bisect import bisect_right
from datetime import date
from pathlib import Path

import numpy as np

import garp_backtest as GB
import garp_data as GD
import garp_index as GI
import mscigarp_data as MD
import mscigarp_index as MI
from stratlib.app import open_context

warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "mscigarp"
SP_OUT = ROOT / "output" / "garp"
CAPITAL = GB.CAPITAL
ETF_COST = 0.0020
MAIN_START, EXT_START, FULL_START = "2015-12-18", "2007-12-21", "2002-12-02"
SWITCH = "2024-06-03"                 # iShares GARP adopted the index
FUND_INCEPTION = "2020-01-16"            # first bar in the price data (fund launched 14 Jan 2020)
OFFICIAL_ANNUAL = {2012: 17.02, 2013: 36.76, 2014: 12.84, 2015: 0.48, 2016: 6.69, 2017: 25.92, 2018: -6.86,
                   2019: 36.74, 2020: 25.95, 2021: 29.07, 2022: -25.52, 2023: 34.93, 2024: 32.13, 2025: 21.77}


def series_on(levels: dict[str, float], days: list[str]) -> np.ndarray:
    keys = sorted(levels)
    out = np.full(len(days), np.nan)
    for i, d in enumerate(days):
        k = bisect_right(keys, d) - 1
        if k >= 0 and keys[0] <= d:
            out[i] = levels[keys[k]]
    return out


def start_at(idx: dict, data: GI.Data, row: int) -> dict:
    """The index as an investor would buy it on `row`: today's drifted weights, then the normal reviews."""
    w = {}
    prev = max(r for r in idx["weights"] if r <= row)
    shares = {j: x / data.px[prev, j] for j, x in idx["weights"][prev].items()}
    tot = sum(s * data.px[row, j] for j, s in shares.items() if data.last[j] >= row)
    for j, s in shares.items():
        if data.last[j] >= row:
            w[j] = s * data.px[row, j] / tot
    weights = {r: x for r, x in idx["weights"].items() if r > row}
    weights[row] = w
    return {**idx, "weights": weights, "start": row}


def rebase(v: np.ndarray, rows: np.ndarray) -> np.ndarray:
    return CAPITAL * v[rows] / v[rows[0]]


def main() -> None:
    ctx = open_context()
    try:
        data = GI.load_data(ctx)
        ex = MI.extra_series(ctx, data.tickers)
        inds = MI.industries_for(ctx, data.tickers)
        tbill = GB.tbill_series(ctx)
        days = [str(d) for d in data.dates]
        off_tr = series_on(MD.levels(ctx.store, "756664", "GRTR"), days)
        off_pr = series_on(MD.levels(ctx.store, "756664", "STRD"), days)
        usa_tr = series_on(MD.levels(ctx.store, "984000", "GRTR"), days)
        bench = {s: GB.etf_total_return(ctx, s, data.dates) for s in ("SPY", "RSP", "IVW", "QQQ", "GARP", "SPGP")}
    finally:
        ctx.close()
    dates = data.dates
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict = {"rules": __doc__, "index_rules": MI.__doc__, "made_on": date.today().isoformat()}

    # --- Rebuild and validation against MSCI's official series
    rebuild = MI.build(data, ex, inds, "2007-11", "proxy")
    rebuild_m = MI.build(data, ex, inds, "2007-11", "methodology")
    val = {"calendar": {}}
    for y in range(2008, 2026):
        a = GI.session_on_or_before(dates, f"{y - 1}-12-31")
        b = GI.session_on_or_before(dates, f"{y}-12-31")
        val["calendar"][y] = {"official": 100 * (off_tr[b] / off_tr[a] - 1),
                              "factsheet": OFFICIAL_ANNUAL.get(y),
                              "rebuilt_proxy": 100 * (rebuild["tr"][b] / rebuild["tr"][a] - 1),
                              "rebuilt_methodology": 100 * (rebuild_m["tr"][b] / rebuild_m["tr"][a] - 1),
                              "msci_usa": 100 * (usa_tr[b] / usa_tr[a] - 1)}
    for name, idx in (("proxy", rebuild), ("methodology", rebuild_m)):
        for a_, b_ in ((EXT_START, days[-1]), (MAIN_START, days[-1]), (SWITCH, days[-1])):
            k = np.array([i for i, d in enumerate(days) if a_ <= d <= b_])
            r1, r2 = np.diff(np.log(idx["tr"][k])), np.diff(np.log(off_tr[k]))
            yrs = (date.fromisoformat(days[k[-1]]) - date.fromisoformat(days[k[0]])).days / 365.25
            val[f"{name} {a_}"] = {"corr": float(np.corrcoef(r1, r2)[0, 1]),
                                   "te": 100 * float((r1 - r2).std() * math.sqrt(252)),
                                   "cagr_rebuilt": 100 * ((idx["tr"][k[-1]] / idx["tr"][k[0]]) ** (1 / yrs) - 1),
                                   "cagr_official": 100 * ((off_tr[k[-1]] / off_tr[k[0]]) ** (1 / yrs) - 1)}
    val["turnover_rebuilt_per_year"] = 4 * float(np.mean(rebuild["turnover"]))
    val["holdings_rebuilt_avg"] = float(np.mean([p["held"] for p in rebuild["plan"]]))
    val["holdings_rebuilt_latest"] = rebuild["plan"][-1]["held"]
    # The fund against the index since it switched
    a = GI.session_on_or_before(dates, SWITCH) - 1
    rr = np.arange(a, len(dates))
    yrs = (date.fromisoformat(days[rr[-1]]) - date.fromisoformat(days[rr[0]])).days / 365.25
    f_c = (bench["GARP"][rr[-1]] / bench["GARP"][rr[0]]) ** (1 / yrs) - 1
    i_c = (off_tr[rr[-1]] / off_tr[rr[0]]) ** (1 / yrs) - 1
    r1, r2 = np.diff(np.log(bench["GARP"][rr])), np.diff(np.log(off_tr[rr]))
    val["fund_since_switch"] = {"fund_cagr": 100 * f_c, "index_cagr": 100 * i_c,
                                "gap": 100 * ((1 + i_c) / (1 + f_c) - 1),
                                "te": 100 * float((r1 - r2).std() * math.sqrt(252))}
    results["validation"] = val

    # --- Versions over a window
    def versions(start: str) -> tuple[dict, np.ndarray, dict]:
        row = GI.session_on_or_before(dates, start)
        rows = np.arange(row, len(dates))
        idx = start_at(rebuild, data, row)
        sim = GB.self_managed(data, idx)
        sim_frac = GB.self_managed(data, idx, whole=False)
        sim_small = GB.self_managed(data, idx, capital=10_000.0)
        sim_big = GB.self_managed(data, idx, capital=1_000_000.0)
        rb = rebase(rebuild["tr"], rows)
        official = rebase(off_tr, rows)
        friction = sim["curve"] / rb
        c = {"MSCI index (official, no costs)": official,
             "Version 1: self-managed": official * friction,
             "Version 2: ETF (0.20%/yr)": GB.etf_curve(off_tr, rows, dates, ETF_COST),
             "ETF at 0.15%/yr (fee only)": GB.etf_curve(off_tr, rows, dates, 0.0015),
             "Self-managed, fractional shares": official * sim_frac["curve"] / rb,
             "Self-managed, $10k": official * sim_small["curve"] * 10 / rb,
             "Self-managed, $1M": official * sim_big["curve"] / 10 / rb,
             "Rebuilt index (no costs)": rb, "Rebuilt index, self-managed": sim["curve"],
             "MSCI USA": rebase(usa_tr, rows)}
        for s in ("SPY", "RSP", "IVW", "QQQ"):
            c[s] = rebase(bench[s], rows)
        return c, rows, {"sim": sim, "idx": idx}

    curves, rows, main_sim = versions(MAIN_START)
    rdays = [days[i] for i in rows]
    spy = curves["SPY"]
    # S&P 500 GARP (ETF version) from the earlier study, same dates
    sp = list(csv.reader(open(SP_OUT / "curves_main.csv")))
    col = sp[0].index("Version 2: ETF (0.35%/yr)")
    spmap = {r[0]: float(r[col]) for r in sp[1:]}
    curves["S&P 500 GARP, ETF (SPGP-style)"] = np.array([spmap[d] for d in rdays]) * CAPITAL / spmap[rdays[0]]

    # Ideas from earlier research, on the ETF version
    ctx = open_context()
    try:
        spy_close = GD.db_bars(ctx.settings.data.db_path, "SPY", "2002-01-01")
    finally:
        ctx.close()
    spy_price = np.array([spy_close[d][3] if d in spy_close else np.nan for d in days])
    for i in range(1, len(days)):
        if not np.isfinite(spy_price[i]):
            spy_price[i] = spy_price[i - 1]
    sma = np.full(len(days), np.nan)
    for i in range(199, len(days)):
        sma[i] = np.mean(spy_price[i - 199:i + 1])
    tdays = sorted(tbill)

    def trend_curve(rows_, tr=None, dd=None, px_=None, sma_=None):
        tr = off_tr if tr is None else tr
        dd = days if dd is None else dd
        px_ = spy_price if px_ is None else px_
        sma_ = sma if sma_ is None else sma_
        out, inv = [CAPITAL * (1 - GB.ETF_SLIP)], True
        for k in range(1, len(rows_)):
            i, h = rows_[k], rows_[k - 1]
            gap = (date.fromisoformat(dd[i]) - date.fromisoformat(dd[h])).days
            if inv:
                v = out[-1] * tr[i] / tr[h] * (1 - ETF_COST) ** (gap / 365.25)
            else:
                rate = tbill[tdays[max(0, bisect_right(tdays, dd[h]) - 1)]] / 100
                v = out[-1] * (1 + rate) ** (gap / 365.25)
            if k + 1 < len(rows_) and dd[rows_[k + 1]][:7] != dd[i][:7] and np.isfinite(sma_[i]):
                want = px_[i] > sma_[i]
                if want != inv:
                    v *= 1 - GB.ETF_SLIP
                    inv = want
            out.append(v)
        return np.array(out)

    def blend_curve(a_curve, b_curve, rows_, dd=None):
        dd = days if dd is None else dd
        parts = [CAPITAL * 0.5, CAPITAL * 0.5]
        out = [CAPITAL]
        for k in range(1, len(rows_)):
            parts[0] *= a_curve[k] / a_curve[k - 1]
            parts[1] *= b_curve[k] / b_curve[k - 1]
            if dd[rows_[k]][:4] != dd[rows_[k - 1]][:4]:
                tot = sum(parts)
                parts = [tot * 0.5 * (1 - 0.0005), tot * 0.5 * (1 - 0.0005)]
            out.append(sum(parts))
        return np.array(out)

    curves["ETF + S&P 200-day trend switch"] = trend_curve(rows)
    curves["50/50 ETF + SPY (rebalanced yearly)"] = blend_curve(curves["Version 2: ETF (0.20%/yr)"], spy, rows)
    curves["50/50 MSCI GARP ETF + S&P 500 GARP ETF"] = blend_curve(curves["Version 2: ETF (0.20%/yr)"],
                                                                   curves["S&P 500 GARP, ETF (SPGP-style)"], rows)

    periods = {"Main: Dec 2015 - now": (rdays[0], rdays[-1]), "In-sample 2016-2021": ("2016-01-01", "2021-12-31"),
               "Out-of-sample 2022-now": ("2022-01-01", rdays[-1]),
               "MSCI simulation (to 31 May 2024)": (rdays[0], "2024-05-31"),
               "Live, held by GARP (3 Jun 2024 - now)": (SWITCH, rdays[-1])}
    table = {}
    for pname, (a_, b_) in periods.items():
        k = [x for x, d in enumerate(rdays) if a_ <= d <= b_]
        sub = [rdays[x] for x in k]
        table[pname] = {c: GB.metrics(v[k], sub, spy[k], tbill) for c, v in curves.items()}
        table[pname]["SPY"] = GB.metrics(spy[k], sub, None, tbill)
    results["periods"] = table

    # --- Full official history (ETF version only) and the extended self-managed run
    # MSCI's series starts in December 2002, before the research price data (2004), so this part has its own
    # calendar: SPY sessions from the index's first day.
    ctx = open_context()
    try:
        fdays = [d for d in sorted(GD.db_bars(ctx.settings.data.db_path, "SPY", FULL_START))]
        fdates = np.array(fdays)
        f_spy = GB.etf_total_return(ctx, "SPY", fdates)
        f_qqq = GB.etf_total_return(ctx, "QQQ", fdates)
        f_off = series_on(MD.levels(ctx.store, "756664", "GRTR"), fdays)
        f_usa = series_on(MD.levels(ctx.store, "984000", "GRTR"), fdays)
    finally:
        ctx.close()
    f_px = np.array([spy_close[d][3] for d in fdays])
    pre = sorted(d for d in spy_close if d < FULL_START)[-199:]       # SPY bars start 30 Sep 2002
    px_all = np.r_[[spy_close[d][3] for d in pre], f_px]
    f_sma = np.full(len(fdays), np.nan)
    for k in range(len(fdays)):
        end = len(pre) + k + 1
        if end >= 200:
            f_sma[k] = np.mean(px_all[end - 200:end])
    full_rows = np.arange(len(fdays))
    full = {"Version 2: ETF (0.20%/yr)": GB.etf_curve(f_off, full_rows, fdates, ETF_COST),
            "MSCI index (official, no costs)": rebase(f_off, full_rows), "MSCI USA": rebase(f_usa, full_rows),
            "SPY": rebase(f_spy, full_rows), "QQQ": rebase(f_qqq, full_rows)}
    full["ETF + S&P 200-day trend switch"] = trend_curve(full_rows, f_off, fdays, f_px, f_sma)
    full["50/50 ETF + SPY (rebalanced yearly)"] = blend_curve(full["Version 2: ETF (0.20%/yr)"], full["SPY"],
                                                             full_rows, fdays)
    fspy = full["SPY"]
    ext_curves, ext_rows, _ = versions(EXT_START)
    edays = [days[i] for i in ext_rows]
    spe = list(csv.reader(open(SP_OUT / "curves_extended.csv")))
    col = spe[0].index("Version 2: ETF (0.35%/yr)")
    spemap = {r[0]: float(r[col]) for r in spe[1:]}
    ext_curves["S&P 500 GARP, ETF (approximate)"] = np.array([spemap[d] for d in edays]) * CAPITAL / spemap[edays[0]]
    hist = {}
    for pname, (a_, b_, cs, sd) in {
            "Full official history: Dec 2002 - now": (fdays[0], fdays[-1], full, fdays),
            "Dec 2002 - Dec 2007": (fdays[0], "2007-12-21", full, fdays),
            "Dec 2007 - now": (edays[0], edays[-1], ext_curves, edays),
            "2008 crisis (to Dec 2009)": (edays[0], "2009-12-31", ext_curves, edays),
            "Dec 2007 - Dec 2015": (edays[0], "2015-12-18", ext_curves, edays)}.items():
        k = [x for x, d in enumerate(sd) if a_ <= d <= b_]
        ref = cs["SPY"]
        hist[pname] = {c: GB.metrics(v[k], [sd[x] for x in k], ref[k], tbill) for c, v in cs.items()
                       if not c.startswith("Self-managed,")}
    results["history"] = hist

    # --- The real fund
    actual = {}
    for label, since in (("GARP since adopting the index", SWITCH), ("GARP since launch (Russell index until Jun 2024)",
                                                                    FUND_INCEPTION)):
        a = GI.session_on_or_before(dates, since)
        rr = np.arange(a, len(dates))
        sd = [days[i] for i in rr]
        actual[label] = {"fund": GB.metrics(bench["GARP"][rr], sd, bench["SPY"][rr], tbill),
                         "etf_version": GB.metrics(GB.etf_curve(off_tr, rr, dates, ETF_COST), sd, bench["SPY"][rr], tbill),
                         "spy": GB.metrics(bench["SPY"][rr], sd, None, tbill)}
    results["actual_fund"] = actual

    # --- Costs and taxes (on the rebuild, like with like)
    sim, idx = main_sim["sim"], main_sim["idx"]
    yrs = (date.fromisoformat(rdays[-1]) - date.fromisoformat(rdays[0])).days / 365.25
    avg_eq = float(np.mean(sim["curve"]))
    results["costs"] = {"slippage": sim["costs"], "slippage_pct_per_year": 100 * sim["costs"] / avg_eq / yrs,
                        "traded_pct_per_year": 100 * sim["traded"] / avg_eq / yrs, "trades": len(sim["trades"]),
                        "reviews": sum(1 for r in idx["weights"]),
                        "friction_pct_per_year": 100 * ((curves["Rebuilt index (no costs)"][-1] / CAPITAL) ** (1 / yrs)
                                                        - (sim["curve"][-1] / CAPITAL) ** (1 / yrs))}
    sim_tax = GB.self_managed(data, idx, taxes=True)
    rb_tr = rebuild["tr"]
    dy = np.zeros(len(dates))
    s0 = rebuild["start"]
    dy[s0 + 1:] = (rb_tr[s0 + 1:] / rb_tr[s0:-1]) / (rebuild["pr"][s0 + 1:] / rebuild["pr"][s0:-1]) - 1
    etf_tax = GB.etf_after_tax(rb_tr, dy, rows, dates, ETF_COST)
    off_dy = np.zeros(len(dates))
    off_dy[1:] = (off_tr[1:] / off_tr[:-1]) / (off_pr[1:] / off_pr[:-1]) - 1
    etf_tax_official = GB.etf_after_tax(off_tr, np.nan_to_num(off_dy), rows, dates, ETF_COST)
    spy_dy = np.zeros(len(dates))
    for i in range(1, len(dates)):
        spy_dy[i] = (bench["SPY"][i] / bench["SPY"][i - 1]) / (spy_price[i] / spy_price[i - 1]) - 1
    spy_tax = GB.etf_after_tax(bench["SPY"], spy_dy, rows, dates, 0.0)
    pre = {"Version 1: self-managed (rebuild)": sim["curve"][-1],
           "Version 2: ETF (rebuild)": GB.etf_curve(rb_tr, rows, dates, ETF_COST)[-1],
           "Version 2: ETF (official index)": curves["Version 2: ETF (0.20%/yr)"][-1], "SPY": spy[-1]}
    tax = {}
    for name, res_, is_fund in (("Version 1: self-managed (rebuild)", sim_tax, False),
                                ("Version 2: ETF (rebuild)", etf_tax, True),
                                ("Version 2: ETF (official index)", etf_tax_official, True), ("SPY", spy_tax, True)):
        held = res_["curve"][-1] - res_["tax_owed_end"]
        sold = held * (1 - (GB.ETF_SLIP if is_fund else 0)) - res_["tax_on_liquidation"]
        tax[name] = {"pre_tax_cagr": 100 * ((pre[name] / CAPITAL) ** (1 / yrs) - 1), "taxes_paid": res_["tax_paid"],
                     "value_held": held, "cagr_held": 100 * ((held / CAPITAL) ** (1 / yrs) - 1),
                     "value_sold": sold, "cagr_sold": 100 * ((sold / CAPITAL) ** (1 / yrs) - 1)}
    tax["Version 1: self-managed (rebuild)"]["log"] = sim_tax.get("tax_log")
    results["taxes"] = tax

    # --- Holdings (rebuild)
    plan = [p for p in rebuild["plan"] if p["effective"] >= "2015-11-01"]
    sectors_avg: dict = {}
    for p in plan:
        for t, w in p["target"].items():
            s = data.sector[data.tickers.index(t)] or "Unknown"
            sectors_avg[s] = sectors_avg.get(s, 0) + w / len(plan)
    names = [set(p["target"]) for p in plan]
    tenure: dict = {}
    for s in names:
        for t in s:
            tenure[t] = tenure.get(t, 0) + 1
    med = {}
    for k in plan[0]["medians"]:
        h = [p["medians"][k][0] for p in plan if p["medians"][k][0] is not None]
        u = [p["medians"][k][1] for p in plan if p["medians"][k][1] is not None]
        med[k] = [float(np.mean(h)), float(np.mean(u))]
    mega = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "BRK-B"]
    results["holdings"] = {
        "reviews": len(plan), "avg_names": float(np.mean([p["held"] for p in plan])),
        "min_names": min(p["held"] for p in plan), "max_names": max(p["held"] for p in plan),
        "distinct": len(tenure), "avg_tenure_reviews": float(np.mean(list(tenure.values()))),
        "kept_per_review": float(np.mean([len(a & b) for a, b in zip(names, names[1:])])),
        "avg_top10": float(np.mean([sum(sorted(p["target"].values(), reverse=True)[:10]) for p in plan])),
        "max_weight": max(max(p["target"].values()) for p in plan),
        "turnover_one_way_per_review": [round(x, 4) for x in rebuild["turnover"][-len(plan) + 1:]],
        "sectors_avg": dict(sorted(sectors_avg.items(), key=lambda x: -x[1])), "medians": med,
        "mega_weight": {p["label"]: round(sum(w for t, w in p["target"].items() if t in mega), 4) for p in plan},
        "latest": {k: v for k, v in plan[-1].items() if k != "scores"}}
    results["contributions"] = GB.contributions(data, rebuild, GI.session_on_or_before(dates, MAIN_START),
                                                len(dates) - 1)

    # --- Files
    json.dump(results, open(OUT / "results.json", "w"), indent=1, default=float)
    for name, cs, sd in (("curves_main.csv", curves, rdays), ("curves_full.csv", full, fdays),
                         ("curves_extended.csv", ext_curves, edays)):
        with open(OUT / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", *cs])
            for k, d in enumerate(sd):
                w.writerow([d, *[round(float(c[k]), 2) for c in cs.values()]])
    with open(OUT / "trades_self_managed_rebuild.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "ticker", "side", "shares_as_traded", "price_as_traded", "reason"])
        w.writerows(sim["trades"])
    with open(OUT / "holdings_rebuild.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["review", "effective", "ticker", "sector", "target_weight", "growth_score", "value_score",
                    "quality_score", "parent_weight"])
        for p in plan:
            for t, wt in sorted(p["target"].items(), key=lambda x: -x[1]):
                s = p["scores"][t]
                w.writerow([p["label"], p["effective"], t, data.sector[data.tickers.index(t)], wt, s["growth"],
                            s["value"], s["quality"], s["parent_weight"]])
    for pname, rows_ in results["periods"].items():
        print(f"\n== {pname}")
        for c, m in rows_.items():
            print(f"{c:44}{m['cagr']:7.2f}%{m.get('excess_cagr', 0):+7.2f}  dd {m['maxdd']:5.1f}  vol {m['vol']:5.1f}"
                  f"  sh {m['sharpe']:.2f}")
    for pname, rows_ in results["history"].items():
        print(f"\n== {pname}")
        for c, m in rows_.items():
            print(f"{c:44}{m['cagr']:7.2f}%{m.get('excess_cagr', 0):+7.2f}  dd {m['maxdd']:5.1f}  sh {m['sharpe']:.2f}")
    print(json.dumps({k: v for k, v in results["validation"].items() if k != "calendar"}, indent=1, default=float))
    print(json.dumps(results["costs"], indent=1, default=float))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "log"} for k, v in results["taxes"].items()},
                     indent=1, default=float))


if __name__ == "__main__":
    main()
