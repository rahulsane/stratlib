"""The MSCI USA Quality GARP Select Index rebuilt from MSCI's methodology (April 2024), on the data provider's prices and statements.

What MSCI does and what this rebuild does instead:

Parent index. MSCI uses the MSCI USA Index (large and mid caps, about 525-600 stocks) with free-float market caps.
Here the parent is the S&P 500 on the review's data date, with full market caps (adjusted close x latest quarterly
basic shares). The S&P 500 covers about 80% of US market value and the MSCI USA about 85%; the stocks missing are
mid caps, which get small weights in a cap-weighted index.

Schedule. Quarterly reviews effective after the close of the last business day of February, May, August and
November, using fundamentals and prices as of the end of January, April, July and October. Weights become index
shares nine sessions before the effective date (when MSCI announces the pro forma).

Growth score. MSCI averages five winsorized (5th/95th percentile) z-scores computed with market-cap-weighted means
and standard deviations: 2 x long-term forward EPS growth, short-term forward EPS growth, internal growth rate
g = ROE x (1 - payout), 5-year historical EPS growth trend and 5-year historical sales-per-share growth trend
(banks and diversified financials skip the sales trend), divided by the number of weights used. The data provider has no
point-in-time analyst estimates, so the two forward variables are missing for every stock, and MSCI's own rule for a
missing variable applies: it is left out and the rest are averaged ("methodology"). The default variant, "proxy",
stands in trailing-year EPS growth for the short-term forward growth; it tracks MSCI's official index more closely
(2008-2026 tracking error 3.0% against 3.4%). A missing growth score is -3.
  Trend = slope of a least-squares line through the last five fiscal-year values / mean of their absolute values.

Value score. Inverses of forward P/E (missing, so trailing P/E per MSCI's substitution rule), P/B and EV/CFO
(EV = market cap + total debt - cash; CFO = trailing four quarters). Financials use E/P and B/P, real estate CFO/EV
only. Each is winsorized and z-scored over the parent, the available z-scores averaged (Appendix III's weights), then
standardized within each sector and clipped to +/-3. Missing = -3.

Quality score. ROE (trailing EPS / book value per share), debt/equity (negated) and earnings variability (standard
deviation of the four year-on-year EPS growth rates in the last five fiscal years, negated), winsorized and z-scored over the parent,
averaged (ROE required plus one other), standardized within sector and clipped to +/-3. Missing = -3.

Selection. Rank by growth score (ties: larger cap first) and take stocks until they cover 50% of the parent's market
cap, including the one that crosses. Buffer: everything up to 35% coverage is in; then current constituents ranked
between 35% and 65% coverage, in growth order, until coverage reaches 50%; then the best remaining.

Weighting. Parent weight x tilt score, normalized. Tilt: 3.5 / 2.5 / 1.5 / 0.5 for the four quarters of
quality coverage (cumulative cap share within the sector, best quality first) when value coverage is within the best
50% of the sector, half that otherwise; doubled for stocks outside the largest names that make up 50% of the selected
cap. Then capped: 5% per issuer, each sector within +/-5 points of its cap-weighted share of the selected stocks,
by MSCI's iterative most-violated-constraint method.

Sectors are the data provider's sectors standing in for GICS. Between reviews: a stock leaving the parent leaves the index and its
value is spread over the rest; no additions; spin-offs not modelled.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

import garp_data as GD
import garp_index as GI
import mscigarp_data as MD

WINSOR = 0.05
SCORE_CLIP = 3.0
MISSING_SCORE = -3.0
TARGET, BUF_LO, BUF_HI = 0.50, 0.35, 0.65
MAX_ISSUER, SECTOR_BAND = 0.05, 0.05
WEIGHTS_LAG = 9                       # sessions between the pro forma weights and the effective date
NO_SALES_TREND = ("Banks", "Capital Markets", "Asset Management", "Credit Services", "Mortgage",
                  "Financial - Conglomerates", "Financial - Diversified", "Shell Companies")
PAYMENTS = {"V", "MA", "PYPL", "FI", "FISV", "FIS", "GPN", "JKHY", "CPAY", "FLT", "WU", "TSS", "XYZ", "SQ"}


# ----------------------------------------------------------------------------------------------
# Extra fundamentals

def quarterly(rows: list[dict], key: str) -> list[tuple]:
    out, seen = [], set()
    for r in sorted((r for r in rows if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        out.append((GI._avail(r, 45), r["date"][:10], GI._num(r.get(key))))
    return out


def extra_series(ctx, tickers: list[str]) -> dict:
    out = {}
    for t in tickers:
        doc = ctx.store.document(MD.EXTRA_KEY + t) or {}
        cash_rows = doc.get("balance_quarter", [])
        cash = quarterly([{**r, "cash": r.get("cashAndShortTermInvestments") or r.get("cashAndCashEquivalents")}
                          for r in cash_rows], "cash")
        out[t] = {"cfo": quarterly(doc.get("cash_quarter", []), "operatingCashFlow"), "cash": cash}
    return out


def _latest(rows, cutoff, max_age=200):
    ok = [r for r in rows if r[0] <= cutoff]
    if ok and (GI._d(cutoff) - GI._d(ok[-1][1])).days <= max_age:
        return ok[-1]
    return None


def _ttm(rows, cutoff):
    ok = [r for r in rows if r[0] <= cutoff]
    if len(ok) < 4 or (GI._d(cutoff) - GI._d(ok[-1][1])).days > 200:
        return None
    last4 = ok[-4:]
    gaps = [(GI._d(b[1]) - GI._d(a[1])).days for a, b in zip(last4, last4[1:])]
    if not all(60 <= g <= 120 for g in gaps) or any(r[2] is None for r in last4):
        return None
    return sum(r[2] for r in last4)


def trend(values: list[float]) -> float | None:
    if len(values) < 5 or any(v is None for v in values):
        return None
    y = np.array(values[-5:], dtype=float)
    scale = np.mean(np.abs(y))
    if scale <= 0:
        return None
    t = np.arange(5, dtype=float)
    slope = np.polyfit(t, y, 1)[0]
    return float(slope / scale)


def variability(values: list[float]) -> float | None:
    """MSCI: sample standard deviation of the four year-on-year EPS growth rates in the last five fiscal years
    (growth measured against the absolute prior-year EPS)."""
    if len(values) < 5 or any(v is None for v in values[-5:]):
        return None
    y = np.array(values[-5:], dtype=float)
    base = np.abs(y[:-1])
    if (base == 0).any():
        return None
    g = (y[1:] - y[:-1]) / base
    return float(np.std(g, ddof=1))


def fundamentals_at(f: GI.Fund, ex: dict, cutoff: str) -> dict:
    out = dict.fromkeys(("shares", "ttm_eps", "ttm_eps_prev", "bvps", "debt", "equity", "cfo", "cash",
                         "eps_trend", "sps_trend", "eps_var"))
    q = [x for x in f.quarters if x[0] <= cutoff]
    if q and (GI._d(cutoff) - GI._d(q[-1][1])).days <= 200:
        out["shares"] = q[-1][3]
    if len(q) >= 4 and (GI._d(cutoff) - GI._d(q[-1][1])).days <= 200:
        last4 = q[-4:]
        gaps = [(GI._d(b[1]) - GI._d(a[1])).days for a, b in zip(last4, last4[1:])]
        if all(60 <= g <= 120 for g in gaps) and all(x[2] is not None for x in last4):
            out["ttm_eps"] = sum(x[2] for x in last4)
        if len(q) >= 8:
            prev4 = q[-8:-4]
            gaps = [(GI._d(b[1]) - GI._d(a[1])).days for a, b in zip(prev4, prev4[1:])]
            if all(60 <= g <= 120 for g in gaps) and all(x[2] is not None for x in prev4):
                out["ttm_eps_prev"] = sum(x[2] for x in prev4)
    b = [x for x in f.balance if x[0] <= cutoff]
    if b and (GI._d(cutoff) - GI._d(b[-1][1])).days <= 200:
        out["debt"], out["equity"] = b[-1][2], b[-1][3]
        if out["equity"] is not None and out["shares"]:
            out["bvps"] = out["equity"] / out["shares"]
    out["cfo"] = _ttm(ex["cfo"], cutoff)
    c = _latest(ex["cash"], cutoff)
    out["cash"] = c[2] if c else None
    ann = [a for a in f.annual if a[0] <= cutoff]
    if ann and (GI._d(cutoff) - GI._d(ann[-1][1])).days <= 550:
        eps = [a[2] for a in ann]
        sps = [a[3] for a in ann]
        out["eps_trend"], out["sps_trend"], out["eps_var"] = trend(eps), trend(sps), variability(eps)
    return out


# ----------------------------------------------------------------------------------------------
# Scoring helpers

def winsor(x: np.ndarray, ok: np.ndarray) -> np.ndarray:
    w = np.full(len(x), np.nan)
    if ok.sum() == 0:
        return w
    lo, hi = np.quantile(x[ok], [WINSOR, 1 - WINSOR], method="nearest")
    w[ok] = np.clip(x[ok], lo, hi)
    return w


def z_plain(x: np.ndarray, ok: np.ndarray, negate=False) -> np.ndarray:
    w = winsor(x, ok)
    z = np.full(len(x), np.nan)
    if ok.sum() < 3:
        return z
    sd = w[ok].std()
    z[ok] = (w[ok] - w[ok].mean()) / sd if sd > 0 else 0.0
    return -z if negate else z


def z_capweighted(x: np.ndarray, ok: np.ndarray, cap: np.ndarray) -> np.ndarray:
    w = winsor(x, ok)
    z = np.full(len(x), np.nan)
    if ok.sum() < 3:
        return z
    c = cap[ok] / cap[ok].sum()
    mu = float(c @ w[ok])
    sd = math.sqrt(float(c @ (w[ok] - mu) ** 2))
    z[ok] = (w[ok] - mu) / sd if sd > 0 else 0.0
    return z


def sector_relative(score: np.ndarray, sectors: np.ndarray) -> np.ndarray:
    out = np.full(len(score), MISSING_SCORE)
    for s in set(sectors):
        k = (sectors == s) & np.isfinite(score)
        if k.sum() >= 2 and score[k].std() > 0:
            out[k] = np.clip((score[k] - score[k].mean()) / score[k].std(), -SCORE_CLIP, SCORE_CLIP)
        elif k.sum():
            out[k] = 0.0
    return out


def mean_available(*cols, weights=None) -> np.ndarray:
    m = np.vstack(cols)
    w = np.ones(len(cols)) if weights is None else np.asarray(weights, dtype=float)
    have = np.isfinite(m)
    num = np.nansum(m * w[:, None], axis=0)
    den = (have * w[:, None]).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def cap_msci(w: np.ndarray, sectors: np.ndarray, target_sector: dict) -> np.ndarray:
    """MSCI's iterative capping: fix the most violated constraint, spread the excess over everyone else."""
    w = w / w.sum()
    upper = {s: v + SECTOR_BAND for s, v in target_sector.items()}
    lower = {s: max(v - SECTOR_BAND, 0.0) for s, v in target_sector.items()}
    for s in lower:                                     # initial relaxation
        lower[s] = min(lower[s], MAX_ISSUER * int((sectors == s).sum()))
    issuer_cap = MAX_ISSUER
    repeats, last_key, stalls = 0, None, 0
    for _ in range(2000):
        worst, key = 1.0, None
        j = int(np.argmax(w))
        if w[j] / issuer_cap > worst:
            worst, key = w[j] / issuer_cap, ("issuer", j)
        for s in target_sector:
            k = sectors == s
            tot = float(w[k].sum())
            if tot / upper[s] > worst:
                worst, key = tot / upper[s], ("sector_max", s)
            if tot > 0 and lower[s] / tot > worst:
                worst, key = lower[s] / tot, ("sector_min", s)
        if key is None or round(worst, 5) <= 1:
            break
        repeats = repeats + 1 if key == last_key else 0
        last_key = key
        if repeats > 10:                                # relax, as MSCI does, a step at a time
            stalls += 1
            if stalls <= 5:
                for s in lower:
                    lower[s] = max(lower[s] - 0.01, 0.0)
            elif stalls <= 10:
                issuer_cap += 0.01
            else:
                for s in upper:
                    upper[s] += 0.01
            repeats = 0
        kind, x = key
        if kind == "issuer":
            excess = w[x] - issuer_cap
            w[x] = issuer_cap
            others = np.ones(len(w), dtype=bool)
            others[x] = False
            w[others] += excess * w[others] / w[others].sum()
        else:
            k = sectors == x
            tot = float(w[k].sum())
            bound = upper[x] if kind == "sector_max" else lower[x]
            delta = tot - bound
            w[k] *= bound / tot
            w[~k] += delta * w[~k] / w[~k].sum()
    return w / w.sum()


# ----------------------------------------------------------------------------------------------

def schedule(dates: np.ndarray, first_year: int = 2005) -> list[dict]:
    out = []
    last = str(dates[-1])
    for y in range(first_year, int(last[:4]) + 1):
        for m in (2, 5, 8, 11):
            eff_month_end = (date(y, m % 12 + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
            data_end = date(y, m, 1) - timedelta(days=1)
            if eff_month_end.isoformat() > last:
                continue
            eff = GI.session_on_or_before(dates, eff_month_end.isoformat())
            out.append({"effective": eff, "weights": max(0, eff - WEIGHTS_LAG),
                        "reference": GI.session_on_or_before(dates, data_end.isoformat()),
                        "cutoff": data_end.isoformat(), "label": f"{y}-{m:02d}"})
    return out


def score(data: GI.Data, ex: dict, industries: list[str], reb: dict, variant: str = "proxy") -> dict:
    ref = reb["reference"]
    ref_day = str(data.dates[ref])
    cand = [j for j in range(len(data.tickers)) if data.member[ref, j] and np.isfinite(data.close[ref, j])]
    groups = {}
    lo = max(0, ref - 19)
    for j in cand:
        key = GD.tokens(data.name[j]) or frozenset([data.tickers[j]])
        dv = float(np.nanmean(data.close[lo:ref + 1, j] * data.volume[lo:ref + 1, j]))
        if key not in groups or dv > groups[key][1]:
            groups[key] = (j, dv)
    cols = np.array(sorted(j for j, _ in groups.values()))
    snaps = [fundamentals_at(data.funds[data.tickers[j]], ex[data.tickers[j]], reb["cutoff"]) for j in cols]

    def arr(k):
        return np.array([np.nan if s[k] is None else s[k] for s in snaps], dtype=float)

    price = data.close[ref, cols]
    shares, ttm, ttm0, bvps, debt, equity, cfo, cash = (arr(k) for k in (
        "shares", "ttm_eps", "ttm_eps_prev", "bvps", "debt", "equity", "cfo", "cash"))
    eps_tr, sps_tr, eps_var = arr("eps_trend"), arr("sps_trend"), arr("eps_var")
    cap = price * shares
    keep = np.isfinite(cap) & (cap > 0)
    cols, price, shares, ttm, ttm0, bvps, debt, equity, cfo, cash, eps_tr, sps_tr, eps_var, cap = (
        a[keep] for a in (cols, price, shares, ttm, ttm0, bvps, debt, equity, cfo, cash, eps_tr, sps_tr, eps_var, cap))
    sectors = np.array([data.sector[j] or "Unknown" for j in cols])
    inds = [industries[j] or "" for j in cols]
    tick = [data.tickers[j] for j in cols]
    pw = cap / cap.sum()

    # Dividends per share over the past year (adjusted, same basis as EPS)
    dps = np.array([float(data.div[max(0, ref - 251):ref + 1, j].sum()) for j in cols])
    roe = np.where((bvps > 0) & np.isfinite(ttm), ttm / bvps, np.nan)
    payout = np.where(ttm > 0, dps / ttm, np.nan)
    g = np.where(np.isfinite(roe) & np.isfinite(payout), roe * (1 - payout), np.nan)

    z_g = z_capweighted(g, np.isfinite(g), cap)
    z_eps = z_capweighted(eps_tr, np.isfinite(eps_tr), cap)
    no_sales = np.array([(s == "Financial Services" and any(k in ind for k in NO_SALES_TREND) and t not in PAYMENTS)
                         for s, ind, t in zip(sectors, inds, tick)])
    sps_ok = np.isfinite(sps_tr) & ~no_sales
    z_sps = z_capweighted(sps_tr, sps_ok, cap)
    if variant == "proxy":
        stg = np.where(np.isfinite(ttm) & np.isfinite(ttm0) & (ttm0 != 0), (ttm - ttm0) / np.abs(ttm0), np.nan)
        z_st = z_capweighted(stg, np.isfinite(stg), cap)
        growth = mean_available(z_st, z_g, z_eps, z_sps)
    else:
        growth = mean_available(z_g, z_eps, z_sps)
    growth = np.where(np.isfinite(growth), growth, MISSING_SCORE)

    # Value (inverse ratios)
    ep = ttm / price
    bp = bvps / price
    ev = cap + np.nan_to_num(debt) - np.nan_to_num(cash)
    cfo_ev = np.where(ev > 0, cfo / ev, np.nan)
    fin, reit = sectors == "Financial Services", sectors == "Real Estate"
    z_ep = z_plain(ep, np.isfinite(ep))
    z_bp = z_plain(bp, np.isfinite(bp))
    z_cf = z_plain(cfo_ev, np.isfinite(cfo_ev))
    value = np.full(len(cols), np.nan)
    for k in range(len(cols)):
        if reit[k]:
            value[k] = z_cf[k]
        elif fin[k]:
            parts = [x for x in (z_ep[k], z_bp[k]) if np.isfinite(x)]
            value[k] = 0.5 * sum(parts) if parts else np.nan
        else:
            parts = [x for x in (z_ep[k], z_bp[k], z_cf[k]) if np.isfinite(x)]
            value[k] = sum(parts) / 3 if parts else np.nan
    value_score = sector_relative(value, sectors)

    # Quality
    de = np.where(equity > 0, debt / equity, np.nan)
    z_roe = z_plain(roe, np.isfinite(roe))
    z_de = z_plain(de, np.isfinite(de), negate=True)
    z_var = z_plain(eps_var, np.isfinite(eps_var), negate=True)
    quality = np.where(np.isfinite(z_roe) & (np.isfinite(z_de) | np.isfinite(z_var)),
                       mean_available(z_roe, z_de, z_var), np.nan)
    quality_score = sector_relative(quality, sectors)
    return {"cols": cols, "cap": cap, "pw": pw, "growth": growth, "value": value_score, "quality": quality_score,
            "sectors": sectors, "tick": tick, "universe": len(cols),
            "raw": {"g": g, "eps_trend": eps_tr, "sps_trend": sps_tr, "roe": roe, "de": de, "eps_var": eps_var,
                    "ep": ep, "bp": bp, "cfo_ev": cfo_ev}}


def select(sc: dict, current: set[int]) -> np.ndarray:
    order = np.lexsort((-sc["pw"], -sc["growth"]))
    cum = np.cumsum(sc["pw"][order])
    if not current:
        n = int(np.searchsorted(cum, TARGET) + 1)
        return order[:n]
    n_lo = int(np.searchsorted(cum, BUF_LO) + 1)
    n_hi = int(np.searchsorted(cum, BUF_HI) + 1)
    chosen = list(order[:n_lo])
    cover = float(sc["pw"][chosen].sum())
    for k in order[n_lo:n_hi]:
        if cover >= TARGET:
            break
        if sc["cols"][k] in current:
            chosen.append(k)
            cover += sc["pw"][k]
    for k in order[n_lo:]:
        if cover >= TARGET:
            break
        if k not in chosen:
            chosen.append(k)
            cover += sc["pw"][k]
    return np.array(chosen)


def tilt_weights(sc: dict, chosen: np.ndarray) -> np.ndarray:
    cap = sc["cap"][chosen]
    sec = sc["sectors"][chosen]
    n = len(chosen)
    by_cap = np.argsort(-cap)
    cum = np.cumsum(cap[by_cap]) / cap.sum()
    top = np.zeros(n, dtype=bool)
    top[by_cap[:int(np.searchsorted(cum, 0.5) + 1)]] = True

    def coverage(score):
        out = np.zeros(n)
        for s in set(sec):
            k = np.flatnonzero(sec == s)
            o = k[np.lexsort((-cap[k], -score[chosen][k]))]
            out[o] = np.cumsum(cap[o]) / cap[o].sum()
        return out

    vc, qc = coverage(sc["value"]), coverage(sc["quality"])
    base = np.select([qc <= 0.25, qc <= 0.50, qc <= 0.75], [3.5, 2.5, 1.5], 0.5)
    tilt = base * np.where(vc <= 0.50, 1.0, 0.5) * np.where(top, 1.0, 2.0)
    raw = sc["pw"][chosen] * tilt
    target_sector = {}
    for s in set(sec):
        target_sector[s] = float(cap[sec == s].sum() / cap.sum())
    return cap_msci(raw, sec, target_sector)


def build(data: GI.Data, ex: dict, industries: list[str], start_label: str, variant: str = "proxy") -> dict:
    """Same output shape as garp_index.build, so the backtest code can run on either index."""
    sched = [r for r in schedule(data.dates) if r["label"] >= start_label]
    n, m = data.px.shape
    plan, current = [], set()
    shares = np.zeros(m)
    level_pr, level_tr = np.full(n, np.nan), np.full(n, np.nan)
    weights_eff, turnover, deletions = {}, [], []
    s0 = sched[0]["effective"]
    level_pr[s0] = level_tr[s0] = 100.0
    by_row = {r["effective"]: r for r in sched}
    for i in range(s0, n):
        if i > s0:
            prev = float(shares @ data.px[i - 1])
            val = float(shares @ data.px[i])
            divs = float(shares @ data.div[i])
            level_pr[i] = level_pr[i - 1] * val / prev
            level_tr[i] = level_tr[i - 1] * (val + divs) / prev
        if i in by_row:
            reb = by_row[i]
            sc = score(data, ex, industries, reb, variant)
            chosen = select(sc, current)
            alive = np.array([(data.member[min(i + 1, n - 1), sc["cols"][k]] or i == n - 1)
                              and data.last[sc["cols"][k]] >= reb["weights"] for k in chosen], dtype=bool)
            chosen = chosen[alive]
            w = tilt_weights(sc, chosen)
            cols = sc["cols"][chosen]
            new = np.zeros(m)
            new[cols] = w / data.px[reb["weights"], cols]
            new *= (float(shares @ data.px[i]) if i > s0 else 100.0) / float(new @ data.px[i])
            if i > s0:
                before = shares * data.px[i] / float(shares @ data.px[i])
                after = new * data.px[i] / float(new @ data.px[i])
                turnover.append(0.5 * float(np.abs(after - before).sum()))
            shares = new
            current = set(int(j) for j in cols)
            weights_eff[i] = {int(j): float(shares[j] * data.px[i, j] / (shares @ data.px[i])) for j in cols}
            medians = {}
            for k, v in sc["raw"].items():
                held_v, all_v = v[chosen], v
                medians[k] = [float(np.nanmedian(held_v)) if np.isfinite(held_v).any() else None,
                              float(np.nanmedian(all_v)) if np.isfinite(all_v).any() else None]
            plan.append({"label": reb["label"], "effective": str(data.dates[i]),
                         "reference": str(data.dates[reb["reference"]]), "universe": sc["universe"],
                         "held": len(cols), "coverage": float(sc["pw"][chosen].sum()), "medians": medians,
                         "target": {data.tickers[j]: round(float(x), 6) for j, x in zip(cols, w)},
                         "scores": {data.tickers[sc["cols"][k]]: {
                             "growth": round(float(sc["growth"][k]), 3), "value": round(float(sc["value"][k]), 3),
                             "quality": round(float(sc["quality"][k]), 3), "parent_weight": round(float(sc["pw"][k]), 6)}
                             for k in chosen}})
            continue
        if i < n - 1:
            held = np.flatnonzero(shares > 0)
            gone = [j for j in held if (data.member[i, j] and not data.member[i + 1, j]) or data.last[j] == i]
            if gone:
                total = float(shares @ data.px[i])
                for j in gone:
                    deletions.append({"date": str(data.dates[i]), "ticker": data.tickers[j],
                                      "weight": float(shares[j] * data.px[i, j] / total)})
                    shares[j] = 0.0
                shares *= total / float(shares @ data.px[i])
    return {"dates": data.dates, "pr": level_pr, "tr": level_tr, "plan": plan, "turnover": turnover,
            "deletions": deletions, "weights": weights_eff, "start": s0}


def industries_for(ctx, tickers: list[str]) -> list[str]:
    res = ctx.store.document(GD.RESOLVED_KEY)["series"]
    return [res.get(t, {}).get("industry") or "" for t in tickers]
