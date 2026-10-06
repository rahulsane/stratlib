"""The S&P 500 GARP Index rebuilt from its published rules (S&P DJI, "S&P GARP Indices Methodology", April 2024).

Rules as implemented (the methodology's own words are in the report):

Schedule. Rebalancing takes effect after the close of the third Friday of June and December (the session before,
when that Friday is a holiday). The universe is the S&P 500 on the reference date, the last session of May and
November. Fundamentals are those available five weeks before the rebalancing date: a statement counts from its
filing date (period end + 45 days for quarters, + 75 days for years, when the data provider has no usable filing date). E/P uses
the reference-date close. Weights become index shares at the closes of the Wednesday before the second Friday of
June and December, so they drift for about a week before taking effect.

Eligibility. Ten months of trading by the reference date; one share class per company (the one with the larger
20-day dollar volume); a growth z-score and a QV z-score. Current rules (from 16 Dec 2022): positive current
fiscal-year EPS, and positive trailing EPS and book value per share. `rules="historical"` drops those two tests
before 16 Dec 2022 and applies the earlier ROE rule (only a ROE made positive by two negatives is replaced),
which is what the official index did; it is used for the check against the published index returns.

Factors (the data provider's statements; per-share figures are restated by the provider for later splits, as are the prices):
  EPS growth   = (1 + (EPS_FY0 - EPS_FY-3) / |EPS_FY-3|)^(1/3) - 1, EPS = net income / diluted shares, fiscal years
  SPS growth   = the same on revenue / diluted shares
  Leverage     = total debt / total stockholders' equity (latest quarter)
  ROE          = trailing-four-quarter EPS / book value per share
  E/P          = trailing-four-quarter EPS / reference-date price
Each factor is winsorized at the 2.5th and 97.5th percentiles of the universe and turned into a z-score with the
universe mean and standard deviation (leverage negated). A ROE or leverage that the rules exclude takes the lowest
z-score of the universe. Growth z = mean of the two growth z-scores (one if the other is missing); QV z = mean of
the available quality z-scores and the E/P z-score (E/P and one quality factor required). Both are clipped to
[-4, 4].

Selection. Top 150 by growth z; of those, the top 60 by QV z, then current constituents ranked 61-90 in QV order,
then the best-ranked others, until there are 75.

Weights. Growth score GS = 1 + z (z > 0) or 1 / (1 - z) (z <= 0), weight proportional to GS; stock weight between
0.05% and 5%, sector weight at most 40% (the data provider's sectors stand in for GICS sectors).

Between rebalancings. Index shares are fixed. A stock leaving the S&P 500 leaves the index at its last close in the
S&P 500 (or its last traded close, if earlier), and its value is spread over the others in proportion to their
weights (the divisor method). There are no additions. Spin-offs are not modelled. Dividends are reinvested across
the index at the ex-date close (total return); the price return ignores them.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import garp_data as GD
from stratlib.app import open_context
from stratlib.sim.panel import _factor_column, clean_bad_bars

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"

GROWTH_COUNT, QV_COUNT, AUTO_COUNT, BUFFER_COUNT = 150, 75, 60, 90
MAX_W, MIN_W, SECTOR_CAP = 0.05, 0.0005, 0.40
WINSOR, Z_CLIP = 0.025, 4.0
DATA_LAG_DAYS = 35
RULE_CHANGE = "2022-12-16"
FIELDS = ("open", "high", "low", "close", "volume")


# ----------------------------------------------------------------------------------------------
# Data

@dataclass
class Data:
    dates: np.ndarray
    tickers: list
    close: np.ndarray          # split-adjusted, NaN where no bar
    px: np.ndarray             # close carried forward (valuation of halted or delisted names)
    volume: np.ndarray
    factor: np.ndarray         # as-traded = adjusted * factor
    div: np.ndarray            # adjusted dividend per adjusted share, on the ex-date row
    member: np.ndarray         # in the S&P 500 that session
    first: np.ndarray          # first row with a bar
    last: np.ndarray           # last row with a bar
    sector: list
    name: list
    funds: dict
    notes: dict = field(default_factory=dict)

    def col(self) -> dict:
        return {t: j for j, t in enumerate(self.tickers)}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _avail(row: dict, lag_days: int) -> str:
    end = row["date"][:10]
    filed = (row.get("filingDate") or row.get("acceptedDate") or "")[:10]
    try:
        if filed and (_d(filed) - _d(end)).days >= 10:
            return filed
    except ValueError:
        pass
    return (_d(end) + timedelta(days=lag_days)).isoformat()


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _per_share(row: dict, value_key: str, eps_key: str | None = None):
    value = _num(row.get(value_key))
    shares = _num(row.get("weightedAverageShsOutDil")) or _num(row.get("weightedAverageShsOut"))
    if value is not None and shares and shares > 0:
        return value / shares
    return _num(row.get(eps_key)) if eps_key else None


@dataclass
class Fund:
    annual: list      # (avail, end, eps, sps)
    quarters: list    # (avail, end, eps, basic shares)
    balance: list     # (avail, end, debt, equity)


def net_revenue(r: dict, financial: bool) -> dict:
    """FMP reports lenders' revenue gross of interest expense (JPMorgan 2024: $271B against $177B net). S&P's data
    (Capital IQ) uses net revenue, so for financial companies earning a quarter or more of revenue as interest, the
    interest expense is taken off."""
    rev, inc, exp = _num(r.get("revenue")), _num(r.get("interestIncome")), _num(r.get("interestExpense"))
    if financial and rev and inc and exp and inc >= 0.25 * rev and 0 < exp < rev:
        r = dict(r)
        r["revenue"] = rev - exp
    return r


def fundamentals(doc: dict, financial: bool = False) -> Fund:
    annual, quarters, balance = [], [], []
    seen = set()
    for r in sorted((r for r in doc.get("income_annual", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        r = net_revenue(r, financial)
        annual.append((_avail(r, 75), r["date"][:10], _per_share(r, "netIncome", "epsDiluted"),
                       _per_share(r, "revenue")))
    seen = set()
    for r in sorted((r for r in doc.get("income_quarter", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        shares = _num(r.get("weightedAverageShsOut")) or _num(r.get("weightedAverageShsOutDil"))
        quarters.append((_avail(r, 45), r["date"][:10], _per_share(r, "netIncome", "epsDiluted"), shares))
    seen = set()
    for r in sorted((r for r in doc.get("balance_quarter", []) if r.get("date")), key=lambda r: r["date"]):
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        balance.append((_avail(r, 45), r["date"][:10], _num(r.get("totalDebt")), _num(r.get("totalStockholdersEquity"))))
    return Fund(annual, quarters, balance)


def cagr3(x0, x1):
    if x0 is None or x1 is None or x0 == 0:
        return None
    return float(np.cbrt(1 + (x1 - x0) / abs(x0)) - 1)


def snapshot(f: Fund, cutoff: str) -> dict:
    out = {"eps0": None, "eps_g": None, "sps_g": None, "ttm_eps": None, "bvps": None, "debt": None, "equity": None}
    ann = [a for a in f.annual if a[0] <= cutoff]
    if ann and (_d(cutoff) - _d(ann[-1][1])).days <= 550:
        fy0 = ann[-1]
        target = _d(fy0[1]) - timedelta(days=round(3 * 365.25))
        old = [a for a in f.annual if abs((_d(a[1]) - target).days) <= 60]
        out["eps0"] = fy0[2]
        if old:
            fy3 = min(old, key=lambda a: abs((_d(a[1]) - target).days))
            out["eps_g"], out["sps_g"] = cagr3(fy3[2], fy0[2]), cagr3(fy3[3], fy0[3])
    q = [x for x in f.quarters if x[0] <= cutoff]
    if len(q) >= 4 and (_d(cutoff) - _d(q[-1][1])).days <= 200:
        last4 = q[-4:]
        gaps = [(_d(b[1]) - _d(a[1])).days for a, b in zip(last4, last4[1:])]
        if all(60 <= g <= 120 for g in gaps) and all(x[2] is not None for x in last4):
            out["ttm_eps"] = sum(x[2] for x in last4)
    b = [x for x in f.balance if x[0] <= cutoff]
    if b and (_d(cutoff) - _d(b[-1][1])).days <= 200:
        out["debt"], out["equity"] = b[-1][2], b[-1][3]
        shares = q[-1][3] if q else None
        if out["equity"] is not None and shares:
            out["bvps"] = out["equity"] / shares
    return out


def load_data(ctx, through: str | None = None, rebuild: bool = False) -> Data:
    db_path = ctx.settings.data.db_path
    cal = GD.calendar_from_db(db_path)
    if through:
        cal = [d for d in cal if d <= through]
    path = CACHE / f"garp_data_{cal[-1]}.npz"
    res = ctx.store.document(GD.RESOLVED_KEY)
    resolved = res["series"]
    tickers = sorted(resolved)
    dates = np.array(cal)
    n, m = len(dates), len(tickers)
    if path.exists() and not rebuild:
        z = np.load(path, allow_pickle=False)
        arrays = {f: z[f] for f in FIELDS}
        factor, div, notes = z["factor"], z["div"], json.loads(str(z["notes"]))
        assert list(z["tickers"]) == tickers, "resolved members changed; rebuild"
    else:
        idx = {d: i for i, d in enumerate(cal)}
        arrays = {f: np.full((n, m), np.nan) for f in FIELDS}
        factor, div = np.ones((n, m)), np.zeros((n, m))
        for j, t in enumerate(tickers):
            for d, bar in GD.bars_for(ctx, t, resolved[t]["source"]).items():
                i = idx.get(d)
                if i is None:
                    continue
                for f, v in zip(FIELDS, bar):
                    arrays[f][i, j] = np.nan if v is None else float(v)
            splits = {r.get("date"): r for r in GD.splits_for(ctx, t) if r.get("date")}
            factor[:, j] = _factor_column(dates, splits, "9999-12-31")
            ddoc = ctx.store.document(GD.DIV_KEY + t) or {}
            for d, amount in ddoc.get("rows", {}).items():
                i = idx.get(d)
                if i is not None and amount:
                    div[i, j] += float(amount)
        until = []
        for j in range(m):
            ok = np.flatnonzero(np.isfinite(arrays["close"][:, j]))
            until.append("" if len(ok) == 0 or ok[-1] >= n - 5 else str(dates[ok[-1]]))
        cleaning = clean_bad_bars(arrays, np.array(until), factor)
        cleaning["instrument_changes"] = [f"{tickers[j]} from {dates[i]}" for j, i in cleaning["instrument_changes"]]
        notes = {"cleaning": cleaning}
        CACHE.mkdir(exist_ok=True)
        np.savez(path, tickers=np.array(tickers), factor=factor, div=div, notes=np.array(json.dumps(notes)),
                 **{f: arrays[f] for f in FIELDS})
    close = arrays["close"]
    valid = np.isfinite(close) & (close > 0)
    close = np.where(valid, close, np.nan)
    px = close.copy()
    for i in range(1, n):
        miss = np.isnan(px[i])
        px[i, miss] = px[i - 1, miss]
    px = np.nan_to_num(px)                  # zero before a stock's first bar (never held then)
    first = np.where(valid.any(axis=0), valid.argmax(axis=0), n)
    last = np.where(valid.any(axis=0), n - 1 - valid[::-1].argmax(axis=0), -1)
    member = np.zeros((n, m), dtype=bool)
    for j, t in enumerate(tickers):
        for a, b in resolved[t]["spans"]:
            lo = bisect_left(cal, a)
            hi = bisect_left(cal, b) if b else n
            member[lo:hi, j] = True
    funds = {}
    for t in tickers:
        doc = ctx.store.document(GD.STMT_KEY + t)
        financial = (resolved[t].get("sector") or "") == "Financial Services"
        funds[t] = fundamentals(doc, financial) if doc else Fund([], [], [])
    sector = [resolved[t].get("sector") or "Unknown" for t in tickers]
    name = [resolved[t].get("name") or "" for t in tickers]
    notes["missing_spans"] = res["missing"]
    notes["renamed"] = res["renamed"]
    members = GD.membership(GD.load_members(ctx))
    notes["sp500_count"] = {str(dates[i]): sum(mm.in_index(str(dates[i])) for mm in members.values())
                            for i in range(0, n, 21)}
    return Data(dates, tickers, close, px, np.nan_to_num(arrays["volume"]), factor, div, member, first, last,
                sector, name, funds, notes)


# ----------------------------------------------------------------------------------------------
# Calendar

def third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def session_on_or_before(dates: np.ndarray, day: str) -> int:
    return int(np.searchsorted(dates, day, side="right")) - 1


def schedule(dates: np.ndarray, first_year: int = 2005) -> list[dict]:
    out = []
    last = str(dates[-1])
    for y in range(first_year, int(last[:4]) + 1):
        for m, ref_m in ((6, 5), (12, 11)):
            eff = third_friday(y, m)
            second_friday = eff - timedelta(days=7)
            weights_day = second_friday - timedelta(days=2)
            ref_day = date(y, ref_m + 1, 1) - timedelta(days=1)
            if eff.isoformat() > last:
                continue
            out.append({
                "effective": session_on_or_before(dates, eff.isoformat()),
                "weights": session_on_or_before(dates, weights_day.isoformat()),
                "reference": session_on_or_before(dates, ref_day.isoformat()),
                "cutoff": (eff - timedelta(days=DATA_LAG_DAYS)).isoformat(),
                "label": f"{y}-{m:02d}",
            })
    return out


# ----------------------------------------------------------------------------------------------
# Scoring

def winsorized_z(x: np.ndarray, ok: np.ndarray, negate: bool = False) -> np.ndarray:
    z = np.full(len(x), np.nan)
    if ok.sum() < 3:
        return z
    v = x[ok]
    lo, hi = np.quantile(v, [WINSOR, 1 - WINSOR], method="nearest")
    w = np.clip(v, lo, hi)
    sd = w.std()
    zz = (w - w.mean()) / sd if sd > 0 else np.zeros_like(w)
    z[ok] = -zz if negate else zz
    return z


def growth_score(z: np.ndarray) -> np.ndarray:
    return np.where(z > 0, 1 + z, 1 / (1 - z))


def cap_weights(raw: np.ndarray, sectors: list[str]) -> np.ndarray:
    w = raw / raw.sum()
    sec = np.array(sectors)
    for _ in range(500):
        old = w.copy()
        w = np.minimum(w, MAX_W)
        capped_sector = np.zeros(len(w), dtype=bool)
        for s in set(sectors):
            k = sec == s
            if s != "Unknown" and w[k].sum() > SECTOR_CAP:
                w[k] *= SECTOR_CAP / w[k].sum()
                capped_sector |= k
        w = np.maximum(w, MIN_W)
        resid = 1 - w.sum()
        if resid > 0:
            free = (w < MAX_W - 1e-12) & ~capped_sector
        else:
            free = (w > MIN_W + 1e-12)
        if not free.any():
            break
        w[free] += resid * w[free] / w[free].sum()
        if np.abs(w - old).max() < 1e-13:
            break
    return w / w.sum()


def score(data: Data, reb: dict, rules: str = "current") -> dict:
    """Factor table, eligibility and composite scores for one rebalancing."""
    ref = reb["reference"]
    ref_day = str(data.dates[ref])
    min_first = (_d(ref_day) - timedelta(days=304)).isoformat()
    cand = [j for j in range(len(data.tickers))
            if data.member[ref, j] and np.isfinite(data.close[ref, j]) and str(data.dates[data.first[j]]) <= min_first]
    # One listing per company: the larger 20-session dollar volume.
    groups: dict = {}
    lo = max(0, ref - 19)
    for j in cand:
        key = GD.tokens(data.name[j]) or frozenset([data.tickers[j]])
        dv = float(np.nanmean(data.close[lo:ref + 1, j] * data.volume[lo:ref + 1, j]))
        if key not in groups or dv > groups[key][1]:
            groups[key] = (j, dv)
    cols = np.array(sorted(j for j, _ in groups.values()))
    snaps = [snapshot(data.funds[data.tickers[j]], reb["cutoff"]) for j in cols]
    price = data.close[ref, cols]

    def arr(k):
        return np.array([np.nan if s[k] is None else s[k] for s in snaps], dtype=float)

    eps0, eps_g, sps_g, ttm, bvps, debt, equity = (arr(k) for k in
                                                  ("eps0", "eps_g", "sps_g", "ttm_eps", "bvps", "debt", "equity"))
    new_rules = rules == "current" or str(data.dates[reb["effective"]]) >= RULE_CHANGE
    roe = ttm / bvps
    roe_have = np.isfinite(ttm) & np.isfinite(bvps) & (bvps != 0)
    if new_rules:
        roe_ok = roe_have & (ttm > 0) & (bvps > 0)
    else:
        roe_ok = roe_have & ~((ttm < 0) & (bvps < 0))
    lev = debt / equity
    lev_have = np.isfinite(debt) & np.isfinite(equity) & (equity != 0)
    lev_ok = lev_have & (equity > 0)
    ep = ttm / price
    ep_ok = np.isfinite(ep)

    z_epsg = winsorized_z(eps_g, np.isfinite(eps_g))
    z_spsg = winsorized_z(sps_g, np.isfinite(sps_g))
    z_roe = winsorized_z(roe, roe_ok)
    z_lev = winsorized_z(lev, lev_ok, negate=True)
    z_ep = winsorized_z(ep, ep_ok)
    if roe_ok.any():
        z_roe[roe_have & ~roe_ok] = np.nanmin(z_roe)
    if lev_ok.any():
        z_lev[lev_have & ~lev_ok] = np.nanmin(z_lev)

    with np.errstate(all="ignore"):
        gz = np.nanmean(np.vstack([z_epsg, z_spsg]), axis=0)
        quality = np.isfinite(z_roe) | np.isfinite(z_lev)
        qv = np.nanmean(np.vstack([z_lev, z_roe, z_ep]), axis=0)
    qv[~(np.isfinite(z_ep) & quality)] = np.nan
    gz, qv = np.clip(gz, -Z_CLIP, Z_CLIP), np.clip(qv, -Z_CLIP, Z_CLIP)
    eligible = np.isfinite(gz) & np.isfinite(qv)
    if new_rules:
        eligible &= (eps0 > 0) & np.isfinite(ttm) & (ttm > 0) & np.isfinite(bvps) & (bvps > 0)
    return {"cols": cols, "eligible": eligible, "gz": gz, "qv": qv, "eps0": eps0, "eps_g": eps_g, "sps_g": sps_g,
            "roe": roe, "lev": lev, "ep": ep, "universe": int(data.member[ref].sum()), "candidates": len(cols)}


def select(sc: dict, current: set[int]) -> list[int]:
    cols, ok = sc["cols"], sc["eligible"]
    idx = np.flatnonzero(ok)
    by_growth = idx[np.lexsort((-sc["qv"][idx], -sc["gz"][idx]))][:GROWTH_COUNT]
    by_qv = by_growth[np.lexsort((-sc["gz"][by_growth], -sc["qv"][by_growth]))]
    chosen = list(by_qv[:AUTO_COUNT])
    for k in by_qv[AUTO_COUNT:BUFFER_COUNT]:
        if len(chosen) < QV_COUNT and cols[k] in current:
            chosen.append(k)
    for k in by_qv[AUTO_COUNT:]:
        if len(chosen) >= QV_COUNT:
            break
        if k not in chosen:
            chosen.append(k)
    return chosen


# ----------------------------------------------------------------------------------------------
# Index

def build(data: Data, start_label: str, rules: str = "current") -> dict:
    """Rebalancing plan and daily price and total-return levels from the first rebalancing at `start_label`."""
    sched = [r for r in schedule(data.dates) if r["label"] >= start_label]
    n, m = data.px.shape
    plan, current = [], set()
    shares = np.zeros(m)
    level_pr = np.full(n, np.nan)
    level_tr = np.full(n, np.nan)
    weights_eff = {}
    turnover = []
    deletions = []
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
            sc = score(data, reb, rules)
            chosen = select(sc, current)
            cols = sc["cols"][chosen]
            alive = np.array([data.member[min(i + 1, n - 1), j] or i == n - 1 for j in cols], dtype=bool)
            alive &= np.array([data.last[j] >= reb["weights"] for j in cols], dtype=bool)
            gz = sc["gz"][chosen][alive]
            cols = cols[alive]
            w = cap_weights(growth_score(gz), [data.sector[j] for j in cols])
            new = np.zeros(m)
            new[cols] = w / data.px[reb["weights"], cols]
            val = float(new @ data.px[i])
            new *= (float(shares @ data.px[i]) if i > s0 else 100.0) / val
            if i > s0:
                before = shares * data.px[i] / float(shares @ data.px[i])
                after = new * data.px[i] / float(new @ data.px[i])
                turnover.append(0.5 * float(np.abs(after - before).sum()))
            shares = new
            current = set(int(j) for j in cols)
            weights_eff[i] = {int(j): float(shares[j] * data.px[i, j] / (shares @ data.px[i])) for j in cols}
            plan.append({"label": reb["label"], "effective": str(data.dates[i]),
                         "reference": str(data.dates[reb["reference"]]), "weights_day": str(data.dates[reb["weights"]]),
                         "cutoff": reb["cutoff"], "universe": sc["universe"], "candidates": sc["candidates"],
                         "eligible": int(sc["eligible"].sum()), "held": len(cols),
                         "target": {data.tickers[j]: round(float(x), 6) for j, x in zip(cols, w)},
                         "scores": {data.tickers[c]: {"gz": round(float(sc["gz"][k]), 3), "qv": round(float(sc["qv"][k]), 3),
                                                      "eps_g": _r(sc["eps_g"][k]), "sps_g": _r(sc["sps_g"][k]),
                                                      "roe": _r(sc["roe"][k]), "lev": _r(sc["lev"][k]), "ep": _r(sc["ep"][k])}
                                    for k, c in zip(chosen, sc["cols"][chosen])},
                         "universe_medians": {k: _r(np.nanmedian(np.where(np.isfinite(sc[k]), sc[k], np.nan)))
                                              for k in ("eps_g", "sps_g", "roe", "lev", "ep")}})
            continue
        # Removals after this session's close: left the S&P 500 (not a member next session) or no more bars.
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


def _r(x):
    x = float(x)
    return round(x, 5) if math.isfinite(x) else None


if __name__ == "__main__":
    import sys
    ctx = open_context()
    try:
        data = load_data(ctx, rebuild="--rebuild" in sys.argv)
    finally:
        ctx.close()
    print(len(data.tickers), "tickers;", data.notes.get("cleaning"))
    res = build(data, "2005-12")
    for p in res["plan"][-3:]:
        print(p["label"], p["universe"], p["candidates"], p["eligible"], p["held"])
