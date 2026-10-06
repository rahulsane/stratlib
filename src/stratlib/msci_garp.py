"""MSCI USA Quality GARP Select: the index's rules as research/mscigarp_index.py rebuilt them from MSCI's methodology
(April 2024). research/output/mscigarp/report.md sets out every step with its formulas.

The live screen (index_scans.py) and the comparable backtest (sim/msci_garp.py) both use these functions, and
tests/test_msci_garp_parity.py holds them to the research code on the same inputs. What the rebuild does where MSCI's
data is not available:

- Parent: the S&P 500 on the review's data date (MSCI uses the MSCI USA Index), full market caps (split-adjusted close
  x latest quarterly basic shares), one listing per company.
- Growth: MSCI averages five winsorized z-scores with cap-weighted means and deviations (2 x long-term forecast EPS
  growth, short-term forecast EPS growth, internal growth g = ROE x (1 - payout), 5-year EPS trend and 5-year sales
  per share trend). FMP has no point-in-time analyst forecasts, so the long-term forecast is missing for every stock
  and MSCI's missing-variable rule drops it; the default "proxy" variant puts trailing-year EPS growth in place of the
  short-term forecast. A missing growth score is -3.
- Value (inverse P/E, P/B and EV/CFO; trailing P/E for the forward one) and quality (ROE, debt/equity and earnings
  variability) are z-scored over the parent, averaged, standardized within each sector and clipped to +/-3.
- Selection: by growth until the stocks cover half the parent's market cap, with a buffer for current members
  (everything to 35% coverage, then current members to 65%, then the best others).
- Weights: parent weight x a tilt from the stock's quality and value rank within its sector (3.5 / 2.5 / 1.5 / 0.5 by
  quality quarter, halved outside the better half by value, doubled outside the largest half by cap), then capped at
  5% per company and each sector within 5 points of its share of the selected stocks' cap.
- Reviews take effect at the last session of February, May, August and November, on fundamentals and prices as of
  the end of the month before; the weights are fixed nine sessions before the effective date.

Sectors are FMP's, standing in for GICS.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

WINSOR = 0.05
SCORE_CLIP = 3.0
MISSING_SCORE = -3.0
WEIGHTS_LAG = 9                       # sessions between the pro forma weights and the effective date
REVIEW_MONTHS = (2, 5, 8, 11)
# Banks and diversified financials skip the sales trend; payment processors are not lenders and keep it.
NO_SALES_TREND = ("Banks", "Capital Markets", "Asset Management", "Credit Services", "Mortgage",
                  "Financial - Conglomerates", "Financial - Diversified", "Shell Companies")
PAYMENTS = {"V", "MA", "PYPL", "FI", "FISV", "FIS", "GPN", "JKHY", "CPAY", "FLT", "WU", "TSS", "XYZ", "SQ"}
# Words that do not tell two companies apart, for matching share classes by name.
STOP = {"inc", "corp", "corporation", "company", "co", "the", "plc", "ltd", "limited", "holdings", "holding",
        "group", "incorporated", "class", "common", "stock", "sa", "nv", "lp", "and", "new", "de", "intl",
        "international", "shares", "ordinary", "series", "llc", "trust", "cl", "com", "ag", "se"}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def num(x):
    """A finite float, or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def tokens(name: str | None) -> frozenset[str]:
    """A company name's distinctive words: share classes of one company have the same tokens."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))
    return frozenset(w for w in words if w not in STOP and len(w) > 1)


# ----------------------------------------------------------------------------------------------
# Statements: what was public at a date

def available(row: dict, lag_days: int) -> str:
    """The first day a statement counts: its filing date, or the period end plus ``lag_days`` when FMP has no usable
    filing date."""
    end = row["date"][:10]
    filed = (row.get("filingDate") or row.get("acceptedDate") or "")[:10]
    try:
        if filed and (_d(filed) - _d(end)).days >= 10:
            return filed
    except ValueError:
        pass
    return (_d(end) + timedelta(days=lag_days)).isoformat()


def _per_share(row: dict, value_key: str, eps_key: str | None = None):
    value = num(row.get(value_key))
    shares = num(row.get("weightedAverageShsOutDil")) or num(row.get("weightedAverageShsOut"))
    if value is not None and shares and shares > 0:
        return value / shares
    return num(row.get(eps_key)) if eps_key else None


@dataclass
class Fund:
    annual: list      # (available, period end, EPS, sales per share)
    quarters: list    # (available, period end, EPS, basic shares)
    balance: list     # (available, period end, total debt, equity)


def net_revenue(r: dict, financial: bool) -> dict:
    """FMP reports lenders' revenue gross of interest expense; for a financial company earning a quarter or more of its
    revenue as interest, the interest expense is taken off."""
    rev, inc, exp = num(r.get("revenue")), num(r.get("interestIncome")), num(r.get("interestExpense"))
    if financial and rev and inc and exp and inc >= 0.25 * rev and 0 < exp < rev:
        r = dict(r)
        r["revenue"] = rev - exp
    return r


def fundamentals(doc: dict, financial: bool = False) -> Fund:
    """A statement bundle (``research:garp:stmts:<symbol>``) as dated per-share series."""
    annual, quarters, balance = [], [], []
    seen = set()
    for r in sorted((r for r in doc.get("income_annual", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        r = net_revenue(r, financial)
        annual.append((available(r, 75), r["date"][:10], _per_share(r, "netIncome", "epsDiluted"), _per_share(r, "revenue")))
    seen = set()
    for r in sorted((r for r in doc.get("income_quarter", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        shares = num(r.get("weightedAverageShsOut")) or num(r.get("weightedAverageShsOutDil"))
        quarters.append((available(r, 45), r["date"][:10], _per_share(r, "netIncome", "epsDiluted"), shares))
    seen = set()
    for r in sorted((r for r in doc.get("balance_quarter", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        balance.append((available(r, 45), r["date"][:10], num(r.get("totalDebt")), num(r.get("totalStockholdersEquity"))))
    return Fund(annual, quarters, balance)


def quarterly(rows: list[dict], key: str) -> list[tuple]:
    out, seen = [], set()
    for r in sorted((r for r in rows if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        out.append((available(r, 45), r["date"][:10], num(r.get(key))))
    return out


def extra_series(doc: dict | None) -> dict:
    """Operating cash flow and cash by quarter from a ``research:mscigarp:extra:<symbol>`` bundle."""
    doc = doc or {}
    cash = quarterly([{**r, "cash": r.get("cashAndShortTermInvestments") or r.get("cashAndCashEquivalents")}
                      for r in doc.get("balance_quarter", [])], "cash")
    return {"cfo": quarterly(doc.get("cash_quarter", []), "operatingCashFlow"), "cash": cash}


def _latest(rows, cutoff, max_age=200):
    ok = [r for r in rows if r[0] <= cutoff]
    if ok and (_d(cutoff) - _d(ok[-1][1])).days <= max_age:
        return ok[-1]
    return None


def _ttm(rows, cutoff):
    ok = [r for r in rows if r[0] <= cutoff]
    if len(ok) < 4 or (_d(cutoff) - _d(ok[-1][1])).days > 200:
        return None
    last4 = ok[-4:]
    gaps = [(_d(b[1]) - _d(a[1])).days for a, b in zip(last4, last4[1:])]
    if not all(60 <= g <= 120 for g in gaps) or any(r[2] is None for r in last4):
        return None
    return sum(r[2] for r in last4)


def trend(values: list[float]) -> float | None:
    """Slope of a least-squares line through the last five values, over the mean of their absolute values."""
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
    """Sample standard deviation of the four year-on-year EPS growth rates in the last five fiscal years (growth
    measured against the absolute prior-year EPS)."""
    if len(values) < 5 or any(v is None for v in values[-5:]):
        return None
    y = np.array(values[-5:], dtype=float)
    base = np.abs(y[:-1])
    if (base == 0).any():
        return None
    g = (y[1:] - y[:-1]) / base
    return float(np.std(g, ddof=1))


def fundamentals_at(f: Fund, ex: dict, cutoff: str) -> dict:
    """The figures a review reads, from statements public on ``cutoff``."""
    out = dict.fromkeys(("shares", "ttm_eps", "ttm_eps_prev", "bvps", "debt", "equity", "cfo", "cash",
                         "eps_trend", "sps_trend", "eps_var"))
    q = [x for x in f.quarters if x[0] <= cutoff]
    if q and (_d(cutoff) - _d(q[-1][1])).days <= 200:
        out["shares"] = q[-1][3]
    if len(q) >= 4 and (_d(cutoff) - _d(q[-1][1])).days <= 200:
        last4 = q[-4:]
        gaps = [(_d(b[1]) - _d(a[1])).days for a, b in zip(last4, last4[1:])]
        if all(60 <= g <= 120 for g in gaps) and all(x[2] is not None for x in last4):
            out["ttm_eps"] = sum(x[2] for x in last4)
        if len(q) >= 8:
            prev4 = q[-8:-4]
            gaps = [(_d(b[1]) - _d(a[1])).days for a, b in zip(prev4, prev4[1:])]
            if all(60 <= g <= 120 for g in gaps) and all(x[2] is not None for x in prev4):
                out["ttm_eps_prev"] = sum(x[2] for x in prev4)
    b = [x for x in f.balance if x[0] <= cutoff]
    if b and (_d(cutoff) - _d(b[-1][1])).days <= 200:
        out["debt"], out["equity"] = b[-1][2], b[-1][3]
        if out["equity"] is not None and out["shares"]:
            out["bvps"] = out["equity"] / out["shares"]
    out["cfo"] = _ttm(ex["cfo"], cutoff)
    c = _latest(ex["cash"], cutoff)
    out["cash"] = c[2] if c else None
    ann = [a for a in f.annual if a[0] <= cutoff]
    if ann and (_d(cutoff) - _d(ann[-1][1])).days <= 550:
        eps = [a[2] for a in ann]
        sps = [a[3] for a in ann]
        out["eps_trend"], out["sps_trend"], out["eps_var"] = trend(eps), trend(sps), variability(eps)
    return out


# ----------------------------------------------------------------------------------------------
# Scoring

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
    num_ = np.nansum(m * w[:, None], axis=0)
    den = (have * w[:, None]).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num_ / den, np.nan)


def score(stocks: list[dict], variant: str = "proxy") -> dict:
    """Growth, value and quality scores for one review's parent.

    Each stock is {"symbol", "sector", "industry", "price" (the reference close), "dps" (dividends per share over the
    past year), "fund" (fundamentals_at)}, one listing per company. Stocks without a positive market cap drop out.
    """
    with np.errstate(invalid="ignore", divide="ignore"):
        return _score(sorted(stocks, key=lambda s: s["symbol"]), variant)


def _score(stocks: list[dict], variant: str) -> dict:

    def arr(k):
        return np.array([np.nan if s["fund"][k] is None else s["fund"][k] for s in stocks], dtype=float)

    price = np.array([s["price"] for s in stocks], dtype=float)
    shares, ttm, ttm0, bvps, debt, equity, cfo, cash = (arr(k) for k in (
        "shares", "ttm_eps", "ttm_eps_prev", "bvps", "debt", "equity", "cfo", "cash"))
    eps_tr, sps_tr, eps_var = arr("eps_trend"), arr("sps_trend"), arr("eps_var")
    dps = np.array([s["dps"] for s in stocks], dtype=float)
    cap = price * shares
    keep = np.isfinite(cap) & (cap > 0)
    index = np.flatnonzero(keep)
    price, shares, ttm, ttm0, bvps, debt, equity, cfo, cash, eps_tr, sps_tr, eps_var, cap, dps = (
        a[keep] for a in (price, shares, ttm, ttm0, bvps, debt, equity, cfo, cash, eps_tr, sps_tr, eps_var, cap, dps))
    symbols = [stocks[k]["symbol"] for k in index]
    sectors = np.array([stocks[k]["sector"] or "Unknown" for k in index])
    inds = [stocks[k]["industry"] or "" for k in index]
    pw = cap / cap.sum() if len(cap) else cap

    roe = np.where((bvps > 0) & np.isfinite(ttm), ttm / bvps, np.nan)
    payout = np.where(ttm > 0, dps / ttm, np.nan)
    g = np.where(np.isfinite(roe) & np.isfinite(payout), roe * (1 - payout), np.nan)

    z_g = z_capweighted(g, np.isfinite(g), cap)
    z_eps = z_capweighted(eps_tr, np.isfinite(eps_tr), cap)
    no_sales = np.array([(s == "Financial Services" and any(k in ind for k in NO_SALES_TREND) and t not in PAYMENTS)
                         for s, ind, t in zip(sectors, inds, symbols)], dtype=bool)
    sps_ok = np.isfinite(sps_tr) & ~no_sales
    z_sps = z_capweighted(sps_tr, sps_ok, cap)
    if variant == "proxy":
        stg = np.where(np.isfinite(ttm) & np.isfinite(ttm0) & (ttm0 != 0), (ttm - ttm0) / np.abs(ttm0), np.nan)
        z_st = z_capweighted(stg, np.isfinite(stg), cap)
        growth = mean_available(z_st, z_g, z_eps, z_sps)
    else:
        growth = mean_available(z_g, z_eps, z_sps)
    growth = np.where(np.isfinite(growth), growth, MISSING_SCORE)

    # Value: inverse ratios
    ep = ttm / price
    bp = bvps / price
    ev = cap + np.nan_to_num(debt) - np.nan_to_num(cash)
    cfo_ev = np.where(ev > 0, cfo / ev, np.nan)
    fin, reit = sectors == "Financial Services", sectors == "Real Estate"
    z_ep = z_plain(ep, np.isfinite(ep))
    z_bp = z_plain(bp, np.isfinite(bp))
    z_cf = z_plain(cfo_ev, np.isfinite(cfo_ev))
    value = np.full(len(symbols), np.nan)
    for k in range(len(symbols)):
        if reit[k]:
            value[k] = z_cf[k]
        elif fin[k]:
            used = [x for x in (z_ep[k], z_bp[k]) if np.isfinite(x)]
            value[k] = 0.5 * sum(used) if used else np.nan
        else:
            used = [x for x in (z_ep[k], z_bp[k], z_cf[k]) if np.isfinite(x)]
            value[k] = sum(used) / 3 if used else np.nan
    value_score = sector_relative(value, sectors)

    # Quality
    de = np.where(equity > 0, debt / equity, np.nan)
    z_roe = z_plain(roe, np.isfinite(roe))
    z_de = z_plain(de, np.isfinite(de), negate=True)
    z_var = z_plain(eps_var, np.isfinite(eps_var), negate=True)
    quality = np.where(np.isfinite(z_roe) & (np.isfinite(z_de) | np.isfinite(z_var)),
                       mean_available(z_roe, z_de, z_var), np.nan)
    quality_score = sector_relative(quality, sectors)
    return {"symbols": symbols, "cap": cap, "pw": pw, "growth": growth, "value": value_score, "quality": quality_score,
            "sectors": sectors, "universe": len(symbols),
            "raw": {"g": g, "eps_trend": eps_tr, "sps_trend": sps_tr, "roe": roe, "de": de, "eps_var": eps_var,
                    "ep": ep, "bp": bp, "cfo_ev": cfo_ev}}


def select(sc: dict, current: set[str], target: float = 0.50, low: float = 0.35, high: float = 0.65) -> np.ndarray:
    """Positions in ``sc`` chosen by growth until they cover ``target`` of the parent's cap; current members (by
    symbol) ranked between ``low`` and ``high`` coverage come before others."""
    order = np.lexsort((-sc["pw"], -sc["growth"]))
    cum = np.cumsum(sc["pw"][order])
    if not current:
        n = int(np.searchsorted(cum, target) + 1)
        return order[:n]
    n_lo = int(np.searchsorted(cum, low) + 1)
    n_hi = int(np.searchsorted(cum, high) + 1)
    chosen = list(order[:n_lo])
    cover = float(sc["pw"][chosen].sum())
    for k in order[n_lo:n_hi]:
        if cover >= target:
            break
        if sc["symbols"][k] in current:
            chosen.append(k)
            cover += sc["pw"][k]
    for k in order[n_lo:]:
        if cover >= target:
            break
        if k not in chosen:
            chosen.append(k)
            cover += sc["pw"][k]
    return np.array(chosen)


def tilts(sc: dict, chosen: np.ndarray) -> np.ndarray:
    """Each chosen stock's tilt: 3.5 / 2.5 / 1.5 / 0.5 by its quality quarter within the sector (by cap coverage),
    halved outside the better half by value, doubled outside the largest names that make up half the selected cap."""
    cap = sc["cap"][chosen]
    sec = sc["sectors"][chosen]
    n = len(chosen)
    by_cap = np.argsort(-cap)
    cum = np.cumsum(cap[by_cap]) / cap.sum()
    top = np.zeros(n, dtype=bool)
    top[by_cap[:int(np.searchsorted(cum, 0.5) + 1)]] = True

    def coverage(values):
        out = np.zeros(n)
        for s in set(sec):
            k = np.flatnonzero(sec == s)
            o = k[np.lexsort((-cap[k], -values[chosen][k]))]
            out[o] = np.cumsum(cap[o]) / cap[o].sum()
        return out

    vc, qc = coverage(sc["value"]), coverage(sc["quality"])
    base = np.select([qc <= 0.25, qc <= 0.50, qc <= 0.75], [3.5, 2.5, 1.5], 0.5)
    return base * np.where(vc <= 0.50, 1.0, 0.5) * np.where(top, 1.0, 2.0)


def cap_weights(w: np.ndarray, sectors: np.ndarray, target_sector: dict, max_issuer: float = 0.05,
                band: float = 0.05) -> np.ndarray:
    """MSCI's iterative capping: fix the most violated constraint, spread the excess over everyone else."""
    w = w / w.sum()
    upper = {s: v + band for s, v in target_sector.items()}
    lower = {s: max(v - band, 0.0) for s, v in target_sector.items()}
    for s in lower:                                     # initial relaxation
        lower[s] = min(lower[s], max_issuer * int((sectors == s).sum()))
    issuer_cap = max_issuer
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


def tilt_weights(sc: dict, chosen: np.ndarray, max_issuer: float = 0.05, band: float = 0.05) -> np.ndarray:
    """Parent weight x tilt, capped."""
    cap, sec = sc["cap"][chosen], sc["sectors"][chosen]
    raw = sc["pw"][chosen] * tilts(sc, chosen)
    target_sector = {s: float(cap[sec == s].sum() / cap.sum()) for s in set(sec)}
    return cap_weights(raw, sec, target_sector, max_issuer, band)


# ----------------------------------------------------------------------------------------------
# Calendar

def session_on_or_before(dates: np.ndarray, day: str) -> int:
    return int(np.searchsorted(dates, day, side="right")) - 1


def month_end(year: int, month: int) -> date:
    return (date(year, month % 12 + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)


def taken_effect(effective: date, last: str) -> bool:
    """Whether a review effective at the month end ``effective`` has taken effect by the session ``last``: the month has
    ended, or only a weekend is left in it."""
    day = date.fromisoformat(last)
    return effective <= day or all((day + timedelta(days=k)).weekday() >= 5 for k in range(1, (effective - day).days + 1))


def schedule(dates: np.ndarray, first_year: int = 2005, weights_lag: int = WEIGHTS_LAG) -> list[dict]:
    """Every review that has taken effect by the last session: rows of the effective session (the last on or before
    the month end), of the weights (``weights_lag`` sessions before) and of the data date (the month before)."""
    out = []
    last = str(dates[-1])
    for y in range(first_year, int(last[:4]) + 1):
        for m in REVIEW_MONTHS:
            effective = month_end(y, m)
            data_end = date(y, m, 1) - timedelta(days=1)
            if not taken_effect(effective, last):
                continue
            eff = session_on_or_before(dates, effective.isoformat())
            if eff < 0 or session_on_or_before(dates, data_end.isoformat()) < 0:
                continue                            # before the first session
            out.append({"effective": eff, "weights": max(0, eff - weights_lag),
                        "reference": session_on_or_before(dates, data_end.isoformat()),
                        "cutoff": data_end.isoformat(), "label": f"{y}-{m:02d}"})
    return out


def next_review(day: str) -> str:
    """The month end on which the next review after ``day`` takes effect (the session on or before it)."""
    y = int(day[:4])
    for year, month in [(y, k) for k in REVIEW_MONTHS] + [(y + 1, REVIEW_MONTHS[0])]:
        end = month_end(year, month)
        if not taken_effect(end, day):
            return end.isoformat()
    raise AssertionError("unreachable")
