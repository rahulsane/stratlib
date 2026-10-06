"""The August 2026 review of the MSCI USA Quality GARP Select Index rebuilt with the inputs MSCI uses, as far as they can
be had, and compared with what the iShares GARP ETF actually holds (a one-off test; nothing here feeds the app).

What changes from mscigarp_index.py's rebuild (which the app's MSCI GARP screen uses):

Parent. The MSCI USA Index's members, from the holdings of the iShares MSCI USA Equal Weighted ETF (EUSA), which holds
the same stocks; every share class is a separate security, as MSCI treats them. GICS sectors from the same file.
Parent weights are free-float market caps: FMP's float shares (shares-float-all) x the data-date close. MSCI's own
free-float weights for the MSCI USA Index (its constituents tool, as of 1 June 2026) check them.

Forecasts. FMP's consensus EPS estimates by fiscal year (analyst-estimates), as of the day this script runs, not as of
the review's data date (31 July 2026). MSCI's formulas:
  EPS0 = the last fiscal year reported by the data date (FMP's consensus row for that year, so that the estimates and
         the base share one "street" basis); EPS1, EPS2 = the next two fiscal years' consensus;
  M    = months from the data date to the end of the fiscal year EPS1 covers;
  EPS12F = (M x EPS1 + (12 - M) x EPS2) / 12,  EPS12B = (M x EPS0 + (12 - M) x EPS1) / 12,
  short-term forward growth = (EPS12F - EPS12B) / |EPS12B|;
  forward P/E = price / EPS12F (the value score uses its inverse; trailing P/E where EPS12F is missing).
MSCI's long-term forward growth is the consensus 3-5 year growth rate, which FMP does not have. Two stand-ins: the
annualized growth from EPS1 to EPS3 ("FY1 to FY3"), and from EPS0 to EPS3 ("FY0 to FY3"), each needing both ends
positive and EPS3 from at least three analysts. The first breaks when the current year holds a one-off: Alphabet's
2026 consensus ($20.61, against $15.15 for 2027) turns its long-term growth negative. Growth score =
(2 x long-term + short-term + internal growth + EPS trend + sales trend) / 6, as MSCI weights it.

Parent at the review. EUSA's holdings are dated after the review, so they include the stocks MSCI USA added in August
(SanDisk among them, which MSCI USA did not hold on 1 June and the fund does not hold). The last version uses the
members of both MSCI's 1 June list and EUSA's: the parent before the August changes, less what it deleted.

Current members (the selection buffer). The index before the review: MSCI's constituents tool as of 1 June 2026 (after
the May review), matched to tickers by name.

Weights. As mscigarp_index.py, except that the 5% cap applies to each issuer (both Alphabet classes together), as
MSCI's methodology says, and sectors are GICS's.

Comparison. The review's weights become shares at the 18 August close (nine sessions before the 31 August effective
date), move with prices to the date of the fund's holdings file, and are compared with the fund's weights: the app's
S&P 500 rebuild (its saved screen), then MSCI's parent without forecasts, with forecasts under each long-term
stand-in, and with the parent before the August changes.

Run: PYTHONPATH=src .venv/Scripts/python research/mscigarp_full.py [--fetch]
  --fetch downloads what is missing (iShares and MSCI files, then about 900 FMP calls); without it the cached inputs
  are used.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import requests

from stratlib import msci_garp as MG
from stratlib.app import open_context
from stratlib.fmp.errors import FMPAuthError, FMPError
from stratlib.index_scans import DIVIDEND_KEY, EXTRA_KEY, STATEMENT_KEY, fetch_company
from stratlib.strategies import latest_screen

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "mscigarp_full"
OUT = ROOT / "notes"            # kept out of research/output: not a published report
ESTIMATES_KEY = "research:mscigarp:estimates:"
FLOAT_KEY = "research:mscigarp:float"
PRICES_KEY = "research:garp:px:"
DATA_DATE, WEIGHTS_DATE, EFFECTIVE = "2026-07-31", "2026-08-18", "2026-08-31"
SOURCES = {
    "eusa.csv": "https://www.ishares.com/us/products/239693/ishares-msci-usa-etf/latest-holdings.csv",
    "garp.csv": "https://www.ishares.com/us/products/312212/ishares-msci-usa-quality-garp-etf/latest-holdings.csv",
}
MSCI_TOOL = ("https://www.msci.com/web/msci/index-tools/constituents?p_p_id=indexconstituents_WAR_indexconstituents_"
             "INSTANCE_9uV8ur27dV1U&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view&p_p_resource_id={code}"
             "&p_p_cacheability=cacheLevelPage")
GICS = {"Information Technology": "Information Technology", "Communication": "Communication Services",
        "Financials": "Financials", "Health Care": "Health Care", "Consumer Discretionary": "Consumer Discretionary",
        "Consumer Staples": "Consumer Staples", "Industrials": "Industrials", "Utilities": "Utilities",
        "Materials": "Materials", "Real Estate": "Real Estate", "Energy": "Energy"}


# ----------------------------------------------------------------------------------------------
# Downloads

def download(fetch: bool) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    targets = {**SOURCES, "msci_756664.json": MSCI_TOOL.format(code=756664), "msci_984000.json": MSCI_TOOL.format(code=984000)}
    for name, url in targets.items():
        path = CACHE / name
        if path.exists() or not fetch:
            continue
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        resp.raise_for_status()
        path.write_text(resp.text, encoding="utf-8")
        print("downloaded", name, len(resp.text))


def holdings(name: str) -> tuple[str, list[dict]]:
    """An iShares holdings file: (as-of date, equity rows)."""
    lines = (CACHE / name).read_text(encoding="utf-8").splitlines()
    as_of = next(l for l in lines if l.startswith("Fund Holdings as of")).split(",", 1)[1].strip('"')
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    rows = [r for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))) if r.get("Asset Class") == "Equity"]
    for r in rows:
        r["symbol"] = r["Ticker"].strip().replace(" ", "-").replace(".", "-")
        r["weight"] = float(r["Weight (%)"].replace(",", ""))
    from datetime import datetime
    return datetime.strptime(as_of, "%b %d, %Y").date().isoformat(), rows


def fmp_fetch(ctx, symbols: list[str]) -> None:
    store, client = ctx.store, ctx.client
    today = date.today()
    # Free float: the bulk list, a page at a time, until every member is found.
    if not store.document(FLOAT_KEY):
        found, wanted = {}, set(symbols)
        for page in range(40):
            rows = client.get("shares-float-all", {"page": page, "limit": 5000}) or []
            for r in rows:
                if r.get("symbol") in wanted:
                    found[r["symbol"]] = {k: r.get(k) for k in ("date", "freeFloat", "floatShares", "outstandingShares")}
            if not rows or wanted <= set(found):
                break
        store.save_document(FLOAT_KEY, {"fetched_on": today.isoformat(), "rows": found})
        print(f"free float: {len(found)} of {len(wanted)} members, {page + 1} pages")
    for k, s in enumerate(symbols):
        if store.document(STATEMENT_KEY + s) is None:
            for prefix, doc in fetch_company(client, s, today).items():
                store.save_document(prefix + s, doc)
        if store.document(ESTIMATES_KEY + s) is None:
            try:
                rows = client.get("analyst-estimates", {"symbol": s, "period": "annual", "limit": 10}) or []
                doc = {"fetched_on": today.isoformat(),
                       "rows": [{k: r.get(k) for k in ("date", "epsAvg", "epsLow", "epsHigh", "numAnalystsEps",
                                                        "revenueAvg")} for r in rows if isinstance(r, dict)]}
            except FMPAuthError:
                raise
            except FMPError as exc:
                doc = {"fetched_on": today.isoformat(), "rows": [], "error": str(exc)}
            store.save_document(ESTIMATES_KEY + s, doc)
        if not store.price_history(s, since=DATA_DATE, through=DATA_DATE) and \
                DATA_DATE not in ((store.document(PRICES_KEY + s) or {}).get("bars") or {}):
            rows = client.get("historical-price-eod/full", {"symbol": s, "from": "2025-06-01", "to": today.isoformat()}) or []
            store.save_document(PRICES_KEY + s, {"fetched_on": today.isoformat(), "bars": {
                r["date"]: [r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("volume")]
                for r in rows if r.get("date") and r.get("close")}})
        if (k + 1) % 100 == 0:
            print(f"  {k + 1}/{len(symbols)}  calls {client.stats.api_calls}", flush=True)
    print("FMP calls:", client.stats.api_calls)


# ----------------------------------------------------------------------------------------------
# Inputs

def closes(store, symbols: list[str], sessions: list[str]) -> dict[str, np.ndarray]:
    """Closes carried forward on the SPY sessions, from the app's prices or the research's FMP bars."""
    out = {}
    rows = {}
    for s, day, *_rest, close, _volume in store.price_panel_rows(symbols, sessions[0], sessions[-1]):
        rows.setdefault(s, {})[day] = close
    for s in symbols:
        bars = rows.get(s) or {d: b[3] for d, b in ((store.document(PRICES_KEY + s) or {}).get("bars") or {}).items()}
        series = np.array([bars.get(d, np.nan) if bars.get(d) else np.nan for d in sessions], dtype=float)
        for i in range(1, len(series)):
            if not np.isfinite(series[i]):
                series[i] = series[i - 1]
        out[s] = series
    return out


def forecasts(doc: dict | None, fund: MG.Fund, cutoff: str) -> dict:
    """EPS0, EPS1, EPS2, M and the two forward growth rates (MSCI's formulas; see the module docstring)."""
    out = {"eps12f": None, "st": None, "lt_fy1": None, "lt_fy0": None}
    rows = sorted((r for r in (doc or {}).get("rows", []) if r.get("date") and r.get("epsAvg") is not None),
                  key=lambda r: r["date"])
    reported = [a for a in fund.annual if a[0] <= cutoff]
    if not rows or not reported:
        return out
    fy0 = reported[-1][1]
    by_year = {r["date"][:7]: r for r in rows}
    base = by_year.get(fy0[:7])
    later = [r for r in rows if r["date"][:7] > fy0[:7]]
    if len(later) < 2:
        return out
    eps0 = base["epsAvg"] if base else reported[-1][2]
    eps1, eps2 = later[0]["epsAvg"], later[1]["epsAvg"]
    if eps0 is None or eps1 is None or eps2 is None:
        return out
    end1 = date.fromisoformat(later[0]["date"][:10])
    cut = date.fromisoformat(cutoff)
    m = min(12, max(0, round((end1 - cut).days / 30.44)))
    f = (m * eps1 + (12 - m) * eps2) / 12
    b = (m * eps0 + (12 - m) * eps1) / 12
    out["eps12f"] = f
    out["st"] = (f - b) / abs(b) if b else None
    if len(later) >= 3 and later[2]["epsAvg"] and later[2]["epsAvg"] > 0 and (later[2].get("numAnalystsEps") or 0) >= 3:
        eps3 = later[2]["epsAvg"]
        out["lt_fy1"] = (eps3 / eps1) ** 0.5 - 1 if eps1 > 0 else None
        out["lt_fy0"] = (eps3 / eps0) ** (1 / 3) - 1 if eps0 > 0 else None
    return out


# MSCI names that share no leading word with iShares' (MSCI name -> ticker).
NAME_ALIASES = {"IBM CORP": "IBM", "HEICO CORP": "HEI", "HEICO CORP A": "HEI-A", "JOHNSON & JOHNSON": "JNJ",
                "CRH  (US)": "CRH", "WABTEC CORP": "WAB", "AMERIPRISE FINANCIAL": "AMP", "PUBLIC SERVICE ENT GRP": "PEG",
                "INTERACTIVE BKRS GRP A": "IBKR", "P G & E CORP": "PCG", "VICI PROPERTIES": "VICI",
                "COGNIZANT TECH SOLUTIONS": "CTSH", "RELIANCE": "RS", "LIBERTY FORMULA ONE C": "FWONK",
                "LYONDELLBASELL INDS A": "LYB", "WP CAREY": "WPC", "ANNALY CAPITAL MGMT": "NLY", "MCCORMICK & CO NV": "MKC",
                "FNF GROUP": "FNF"}
ABBREVIATIONS = {"ENTMT": "ENTERTAINMENT", "FINL": "FINANCIAL", "COS": "COMPANIES", "EXCH": "EXCHANGE",
                 "INTL": "INTERNATIONAL", "HLDGS": "HOLDINGS", "TECH": "TECHNOLOGIES", "SVCS": "SERVICES"}


def name_key(name: str) -> tuple[list[str], str]:
    """(distinctive words, share class letter) of a security name."""
    text = name.upper().replace("'", "").replace(".", "").replace("&", " AND ")
    raw = [ABBREVIATIONS.get(w, w) for w in "".join(c if c.isalnum() else " " for c in text).split()]
    letter = raw[-1] if len(raw) > 1 and len(raw[-1]) == 1 else ""
    words = [w.lower() for w in raw if w.lower() not in MG.STOP and len(w) > 1]
    return words, letter


def same_word(a: str, b: str) -> bool:
    """Equal, or one a prefix of the other of at least four letters (INSTRUMENT, INSTRUMENTS; EXXON, EXXONMOBIL)."""
    short, long = sorted((a, b), key=len)
    return a == b or (len(short) >= 4 and long.startswith(short))


def similarity(a: list[str], b: list[str]) -> float:
    shared = sum(any(same_word(x, y) for y in b) for x in a)
    return shared / (len(a) + len(b) - shared) if a and b else 0.0


def match_names(names: list[str], members: list[dict]) -> tuple[dict[str, str], list[str]]:
    """MSCI security names -> member tickers: the most words in common (at least half), then the share class."""
    keyed = [(name_key(m["Name"]), m["symbol"]) for m in members]
    symbols = {m["symbol"] for m in members}
    found, missing = {}, []
    for n in names:
        if NAME_ALIASES.get(n) in symbols:
            found[n] = NAME_ALIASES[n]
            continue
        words, letter = name_key(n)
        scored = [(similarity(words, other), other_letter == letter, s) for (other, other_letter), s in keyed]
        scored = [x for x in scored if x[0] >= 0.5]
        if not scored:
            missing.append(n)
            continue
        best = max(x[:2] for x in scored)
        top = [x for x in scored if x[:2] == best]
        if len(top) == 1:
            found[n] = top[0][2]
        else:
            missing.append(n)
    return found, missing


# ----------------------------------------------------------------------------------------------
# Scores and weights

def score(stocks: list[dict], use_forecasts: bool, lt_key: str = "lt_fy0") -> dict:
    with np.errstate(invalid="ignore", divide="ignore"):
        return _score(sorted(stocks, key=lambda s: s["symbol"]), use_forecasts, lt_key)


def _score(stocks: list[dict], use_forecasts: bool, lt_key: str) -> dict:
    def arr(k, source="fund"):
        return np.array([np.nan if s[source].get(k) is None else s[source][k] for s in stocks], dtype=float)

    price = np.array([s["price"] for s in stocks])
    cap = np.array([s["float_cap"] for s in stocks])
    keep = np.isfinite(cap) & (cap > 0) & np.isfinite(price)
    stocks = [s for s, k in zip(stocks, keep) if k]
    price, cap = price[keep], cap[keep]
    ttm, ttm0, bvps, debt, equity, cfo, cash = (arr(k) for k in ("ttm_eps", "ttm_eps_prev", "bvps", "debt", "equity",
                                                                  "cfo", "cash"))
    eps_tr, sps_tr, eps_var = arr("eps_trend"), arr("sps_trend"), arr("eps_var")
    st, lt, eps12f = arr("st", "fc"), arr(lt_key, "fc"), arr("eps12f", "fc")
    dps = np.array([s["dps"] for s in stocks])
    sectors = np.array([s["sector"] for s in stocks])
    symbols = [s["symbol"] for s in stocks]
    pw = cap / cap.sum()

    roe = np.where((bvps > 0) & np.isfinite(ttm), ttm / bvps, np.nan)
    payout = np.where(ttm > 0, dps / ttm, np.nan)
    g = np.where(np.isfinite(roe) & np.isfinite(payout), roe * (1 - payout), np.nan)
    z_g = MG.z_capweighted(g, np.isfinite(g), cap)
    z_eps = MG.z_capweighted(eps_tr, np.isfinite(eps_tr), cap)
    no_sales = np.array([sec == "Financials" and any(k in s["industry"] for k in MG.NO_SALES_TREND)
                         and s["symbol"] not in MG.PAYMENTS for sec, s in zip(sectors, stocks)], dtype=bool)
    z_sps = MG.z_capweighted(sps_tr, np.isfinite(sps_tr) & ~no_sales, cap)
    if use_forecasts:
        z_st = MG.z_capweighted(st, np.isfinite(st), cap)
        z_lt = MG.z_capweighted(lt, np.isfinite(lt), cap)
        growth = MG.mean_available(z_lt, z_st, z_g, z_eps, z_sps, weights=[2, 1, 1, 1, 1])
    else:
        trailing = np.where(np.isfinite(ttm) & np.isfinite(ttm0) & (ttm0 != 0), (ttm - ttm0) / np.abs(ttm0), np.nan)
        growth = MG.mean_available(MG.z_capweighted(trailing, np.isfinite(trailing), cap), z_g, z_eps, z_sps)
    growth = np.where(np.isfinite(growth), growth, MG.MISSING_SCORE)

    ep = np.where(use_forecasts & np.isfinite(eps12f), eps12f / price, ttm / price)
    bp = bvps / price
    ev = cap + np.nan_to_num(debt) - np.nan_to_num(cash)
    cfo_ev = np.where(ev > 0, cfo / ev, np.nan)
    z_ep, z_bp, z_cf = (MG.z_plain(x, np.isfinite(x)) for x in (ep, bp, cfo_ev))
    value = np.full(len(symbols), np.nan)
    for k, sec in enumerate(sectors):
        if sec == "Real Estate":
            value[k] = z_cf[k]
        else:
            used = [x for x in ((z_ep[k], z_bp[k]) if sec == "Financials" else (z_ep[k], z_bp[k], z_cf[k])) if np.isfinite(x)]
            value[k] = sum(used) / (2 if sec == "Financials" else 3) if used else np.nan
    de = np.where(equity > 0, debt / equity, np.nan)
    z_roe, z_de, z_var = MG.z_plain(roe, np.isfinite(roe)), MG.z_plain(de, np.isfinite(de), negate=True), \
        MG.z_plain(eps_var, np.isfinite(eps_var), negate=True)
    quality = np.where(np.isfinite(z_roe) & (np.isfinite(z_de) | np.isfinite(z_var)), MG.mean_available(z_roe, z_de, z_var),
                       np.nan)
    return {"symbols": symbols, "cap": cap, "pw": pw, "growth": growth, "value": MG.sector_relative(value, sectors),
            "quality": MG.sector_relative(quality, sectors), "sectors": sectors,
            "issuer": np.array([s["issuer"] for s in stocks]), "st": st, "lt": lt}


def issuer_capped(sc: dict, chosen: np.ndarray, max_issuer: float = 0.05, band: float = 0.05) -> np.ndarray:
    """Tilted weights, capped with MSCI's procedure on issuers (classes of one company together), then split back."""
    raw = sc["pw"][chosen] * MG.tilts(sc, chosen)
    issuers = sc["issuer"][chosen]
    names = sorted(set(issuers))
    total = np.array([raw[issuers == i].sum() for i in names])
    sector = np.array([sc["sectors"][chosen][issuers == i][0] for i in names])
    cap = sc["cap"][chosen]
    target = {s: float(cap[sc["sectors"][chosen] == s].sum() / cap.sum()) for s in set(sector)}
    capped = MG.cap_weights(total, sector, target, max_issuer, band)
    out = np.zeros(len(chosen))
    for k, i in enumerate(names):
        mask = issuers == i
        out[mask] = capped[k] * raw[mask] / raw[mask].sum()
    return out


def drifted(weights: dict[str, float], px: dict[str, np.ndarray], frm: int, to: int) -> dict[str, float]:
    value = {s: w / px[s][frm] * px[s][to] for s, w in weights.items() if np.isfinite(px[s][frm]) and px[s][frm] > 0}
    total = sum(value.values())
    return {s: 100 * v / total for s, v in value.items()}


def compare(ours: dict[str, float], fund: dict[str, float]) -> dict:
    both = set(ours) & set(fund)
    overlap = sum(min(ours[s], fund[s]) for s in both) / 100
    return {"held": len(ours), "fund": len(fund), "common": len(both), "weight_overlap": overlap,
            "missed": sorted(((s, fund[s]) for s in fund if s not in ours), key=lambda x: -x[1]),
            "extra": sorted(((s, ours[s]) for s in ours if s not in fund), key=lambda x: -x[1]),
            "abs_gap": sum(abs(ours.get(s, 0) - fund.get(s, 0)) for s in set(ours) | set(fund)) / 2}


# ----------------------------------------------------------------------------------------------

def main(fetch: bool) -> None:
    download(fetch)
    eusa_date, eusa = holdings("eusa.csv")
    fund_date, fund_rows = holdings("garp.csv")
    fund = {r["symbol"]: r["weight"] for r in fund_rows}
    fund_total = sum(fund.values())
    fund = {s: 100 * w / fund_total for s, w in fund.items()}
    symbols = sorted(r["symbol"] for r in eusa)
    ctx = open_context()
    try:
        if fetch:
            fmp_fetch(ctx, symbols)
        store = ctx.store
        sessions = [b.date for b in store.price_history("SPY", since="2025-06-01", through=fund_date)]
        ref, wgt, last = sessions.index(DATA_DATE), sessions.index(WEIGHTS_DATE), sessions.index(fund_date)
        px = closes(store, symbols, sessions)
        floats = (store.document(FLOAT_KEY) or {}).get("rows", {})
        industries = store.sectors()
        issuer_of = {r["symbol"]: " ".join(sorted(MG.tokens(r["Name"]))) or r["symbol"] for r in eusa}
        stocks, notes = [], {"no_float": [], "no_price": [], "no_statements": [], "no_forecasts": []}
        dates = set(sessions[max(0, ref - 251):ref + 1])
        for r in eusa:
            s = r["symbol"]
            doc = store.document(STATEMENT_KEY + s)
            sector = GICS.get(r["Sector"], r["Sector"])
            fund_ = MG.fundamentals(doc, sector == "Financials") if doc else MG.Fund([], [], [])
            snap = MG.fundamentals_at(fund_, MG.extra_series(store.document(EXTRA_KEY + s)), DATA_DATE)
            fc = forecasts(store.document(ESTIMATES_KEY + s), fund_, DATA_DATE)
            fl = floats.get(s) or {}
            price = px[s][ref]
            float_shares = fl.get("floatShares")
            if not float_shares and snap["shares"] and fl.get("freeFloat"):
                float_shares = snap["shares"] * fl["freeFloat"] / 100
            notes["no_float"] += [] if float_shares else [s]
            notes["no_price"] += [] if np.isfinite(price) else [s]
            notes["no_statements"] += [] if doc else [s]
            notes["no_forecasts"] += [] if fc["st"] is not None else [s]
            dps = sum(v for d, v in ((store.document(DIVIDEND_KEY + s) or {}).get("rows") or {}).items() if d in dates and v)
            stocks.append({"symbol": s, "sector": sector, "industry": industries.get(s, ("", ""))[1], "issuer": issuer_of[s],
                           "price": price, "float_cap": price * float_shares if float_shares else np.nan, "dps": dps,
                           "fund": snap, "fc": fc})
        # Dual-class companies: FMP may report the company's whole float for each class; split it by the classes'
        # shares of MSCI's own free-float weights.
        msci_usa = {c["security_name"]: float(c["security_weight"])
                    for c in json.loads((CACHE / "msci_984000.json").read_text())["constituents"]}
        usa_names, usa_missing = match_names(list(msci_usa), eusa)
        msci_weight = {usa_names[n]: w for n, w in msci_usa.items() if n in usa_names}
        by_issuer = {}
        for st in stocks:
            by_issuer.setdefault(st["issuer"], []).append(st)
        for group in by_issuer.values():
            if len(group) > 1 and len({round(x["float_cap"] / x["price"]) for x in group if np.isfinite(x["float_cap"])}) == 1:
                total = sum(msci_weight.get(x["symbol"], 0) for x in group)
                for x in group:
                    x["float_cap"] *= (msci_weight.get(x["symbol"], 0) / total) if total else 1 / len(group)
        # How well the free-float caps reproduce MSCI's weights (June, so prices differ).
        caps = {st["symbol"]: st["float_cap"] for st in stocks if np.isfinite(st["float_cap"])}
        common = [s for s in caps if s in msci_weight]
        float_check = {"matched": len(common), "of_msci": len(msci_usa)}
        if len(common) > 2:
            ours_w = np.array([caps[s] for s in common]) / sum(caps[s] for s in common)
            msci_w = np.array([msci_weight[s] for s in common]) / sum(msci_weight[s] for s in common)
            ratio = ours_w / msci_w
            float_check.update(corr=float(np.corrcoef(ours_w, msci_w)[0, 1]), median_ratio=float(np.median(ratio)),
                               p10_p90=[float(np.percentile(ratio, 10)), float(np.percentile(ratio, 90))])
        # The index before the review (after May), by name.
        may = [c["security_name"] for c in json.loads((CACHE / "msci_756664.json").read_text())["constituents"]]
        may_names, may_missing = match_names(may, eusa)
        current = set(may_names.values())
        june = set(usa_names.values())
        added = sorted(st["symbol"] for st in stocks if st["symbol"] not in june)     # joined MSCI USA after 1 June
        results = {}
        versions = (("MSCI USA parent, no forecasts", False, "lt_fy0", None),
                    ("With forecasts, long-term FY1 to FY3", True, "lt_fy1", None),
                    ("With forecasts, long-term FY0 to FY3", True, "lt_fy0", None),
                    ("FY0 to FY3, parent before the August changes", True, "lt_fy0", june))
        for label, use, lt_key, parent in versions:
            pool = [st for st in stocks if parent is None or st["symbol"] in parent]
            sc = score(pool, use, lt_key)
            chosen = MG.select(sc, {s for s in current if s in set(sc["symbols"])})
            w = issuer_capped(sc, chosen)
            target = {sc["symbols"][k]: float(x) for k, x in zip(chosen, w)}
            now = drifted(target, px, wgt, last)
            results[label] = {**compare(now, fund), "coverage": float(sc["pw"][chosen].sum()), "parent": len(pool),
                              "top": sorted(now.items(), key=lambda x: -x[1])[:15], "weights": now}
        screen = latest_screen(store, "msci_garp")
        app = {c["symbol"]: c["weight_pct"] for c in screen["candidates"]} if screen else {}
        app = {s: 100 * w / sum(app.values()) for s, w in app.items()}
        results = {"App's rebuild (S&P 500, no forecasts)": compare(app, fund), **results}
        report = {"made_on": date.today().isoformat(), "eusa_date": eusa_date, "fund_date": fund_date,
                  "parent": len(stocks), "float_check": float_check, "msci_usa_unmatched": usa_missing,
                  "may_matched": len(current), "may_unmatched": may_missing, "added_since_june": added,
                  "inputs_missing": {k: v for k, v in notes.items()}, "results": results}
        (OUT / "mscigarp_full_recipe_2026-08.json").write_text(json.dumps(report, indent=1, default=float), encoding="utf-8")
        summary(report)
    finally:
        ctx.close()


def summary(report: dict) -> None:
    print(f"parent {report['parent']} (EUSA {report['eusa_date']}), fund holdings {report['fund_date']}")
    print("free-float check:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in report["float_check"].items()})
    print("MSCI USA names unmatched:", len(report["msci_usa_unmatched"]), report["msci_usa_unmatched"][:12])
    print("May constituents matched:", report["may_matched"], "unmatched:", report["may_unmatched"])
    print("EUSA members not in MSCI USA on 1 June:", report["added_since_june"])
    print("inputs missing:", {k: len(v) for k, v in report["inputs_missing"].items()})
    for label, r in report["results"].items():
        print(f"\n{label}: parent {r.get('parent', '-')}, held {r['held']} vs fund {r['fund']}, common {r['common']}, "
              f"weight overlap {100 * r['weight_overlap']:.0f}%")
        print("  missed:", [(s, round(w, 2)) for s, w in r["missed"][:12]])
        print("  extra: ", [(s, round(w, 2)) for s, w in r["extra"][:12]])


if __name__ == "__main__":
    main("--fetch" in sys.argv)
