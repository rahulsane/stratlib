"""Scan strategies that read company statements: Tom Nash's quality screen and the Traveling Trader's checklist.

The rules are ports of research/nash_screen.py, research/nash_screen_clean.py and research/tt_quality.py
(tests/test_fundamental_parity.py runs both on the same statements). They read the statement bundles the
research harness caches as ``research:nash2:stmts:<symbol>`` (80 quarters of income, balance-sheet, cash-flow
and key-metrics rows), and this module refreshes those bundles from FMP when a screen is run online, so the
harness and the app share one cache. A bundle is refetched when its company has reported earnings since it was
fetched, or when it is older than REFRESH_DAYS.

Statements are used from their filing date (45 days after the period end when FMP's filing date is within ten
days of it), and a stock whose newest statement is older than ``stale_days`` cannot be evaluated.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from . import scanning
from .fmp import FMPAuthError, FMPError
from .scanning import ScanInput, ScanResult, Scanner, _identity, _num

log = logging.getLogger(__name__)

STATEMENT_KEY = "research:nash2:stmts:"
QUARTERS = 80
REFRESH_DAYS = 120
FIELDS = {
    "income": ("date", "period", "fiscalYear", "filingDate", "reportedCurrency", "revenue", "grossProfit",
               "operatingIncome", "operatingExpenses", "netIncome", "incomeBeforeTax", "incomeTaxExpense",
               "weightedAverageShsOutDil"),
    "balance": ("date", "filingDate", "reportedCurrency", "cashAndShortTermInvestments", "totalDebt",
                "capitalLeaseObligations", "totalStockholdersEquity", "totalAssets"),
    "cash": ("date", "filingDate", "reportedCurrency", "operatingCashFlow", "capitalExpenditure", "freeCashFlow",
             "stockBasedCompensation"),
    "metrics": ("date", "reportedCurrency", "marketCap", "enterpriseValue"),
}
ENDPOINTS = {"income": "income-statement", "balance": "balance-sheet-statement", "cash": "cash-flow-statement",
             "metrics": "key-metrics"}

# Rough units per US dollar; only used for the prior-year revenue floor (research/nash_screen_clean.py).
FX = {"USD": 1, "CNY": 6.8, "CAD": 1.3, "EUR": 0.9, "BRL": 4.5, "GBP": 0.78, "JPY": 120, "MXN": 19, "AUD": 1.4,
      "ZAR": 15, "RUB": 70, "INR": 75, "TWD": 30, "KRW": 1200, "HKD": 7.8, "DKK": 6.7, "SGD": 1.36, "ARS": 100,
      "VND": 23000, "IDR": 14500, "TRY": 15, "CHF": 0.95, "KZT": 450, "SEK": 10, "COP": 4000}
CYCLICAL_SECTORS = {"Energy", "Basic Materials", "Consumer Cyclical", "Real Estate", "Industrials"}
INDUSTRIAL_KEEP = {"Aerospace & Defense", "Waste Management"}
CYCLICAL_TECH = {"Semiconductors", "Hardware, Equipment & Parts", "Computer Hardware", "Consumer Electronics",
                 "Technology Distributors", "Communication Equipment"}


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _float(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else np.nan


def available_from(row: dict) -> str:
    """The first day a quarter's statements may be used."""
    period_end = _d(row["date"])
    filed = _d(row["filingDate"]) if row.get("filingDate") else None
    if filed is None or (filed - period_end).days < 10:
        return (period_end + timedelta(days=45)).isoformat()
    return filed.isoformat()


# ----------------------------------------------------------------------------------------------
# Statement bundles: the research harness's cache, refreshed from FMP

def load_statements(store, symbol: str) -> dict | None:
    doc = store.document(STATEMENT_KEY + symbol)
    return doc if doc and "error" not in doc else None


def fetch_statements(client, symbol: str, today: date) -> dict:
    """Four calls: income, balance sheet, cash flow and key metrics, 80 quarters each."""
    doc = {"fetched_on": today.isoformat()}
    try:
        for kind, path in ENDPOINTS.items():
            rows = client.get(path, {"symbol": symbol, "period": "quarter", "limit": QUARTERS})
            if not isinstance(rows, list):
                raise FMPError(f"unexpected {kind} response")
            doc[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in rows if isinstance(r, dict)]
    except FMPAuthError:
        raise
    except FMPError as exc:
        return {"fetched_on": today.isoformat(), "error": str(exc)}
    return doc


def statements_due(store, client, symbols: list[str], today: date, *, progress=None, key: str = STATEMENT_KEY) -> list[str]:
    """Symbols whose bundle (under ``key``) is missing, failed, old, or older than a release of theirs (one calendar
    call per week)."""
    docs = {s: store.document(key + s) for s in symbols}
    fetched = lambda d: d.get("fetched_on") or "0001-01-01"  # noqa: E731  (a bundle without a date is always due)
    due = {s for s, d in docs.items() if d is None or ("error" in d and fetched(d) < today.isoformat())
           or (today - _d(fetched(d))).days > REFRESH_DAYS}
    fresh = {s: fetched(d) for s, d in docs.items() if s not in due}
    if fresh:
        oldest = _d(min(fresh.values()))
        start = max(oldest, today - timedelta(days=REFRESH_DAYS))
        week = start - timedelta(days=start.weekday())
        total_weeks = max(1, int((today - week).days / 7) + 1)
        released: dict[str, str] = {}
        while week <= today:
            for row in client.earnings_calendar(week, week + timedelta(days=6)) or []:
                if isinstance(row, dict) and row.get("symbol") in fresh and row.get("date"):
                    released[row["symbol"]] = max(released.get(row["symbol"], ""), str(row["date"])[:10])
            week += timedelta(days=7)
            if progress:
                progress("Checking earnings releases", min(int((week - start).days / 7), total_weeks), total_weeks)
        due |= {s for s, day in released.items() if fresh[s] <= day <= today.isoformat()}
    return sorted(due)


def refresh_statements(store, client, symbols: list[str], today: date, *, workers: int = 4, progress=None) -> dict:
    """Fetch what is due and save it; a failed fetch never replaces a good bundle."""
    todo = statements_due(store, client, symbols, today, progress=progress)
    done = errors = 0
    if todo:
        with ThreadPoolExecutor(max(1, workers)) as pool:
            for symbol, doc in zip(todo, pool.map(lambda s: fetch_statements(client, s, today), todo)):
                if "error" in doc and load_statements(store, symbol) is not None:
                    errors += 1
                    continue
                store.save_document(STATEMENT_KEY + symbol, doc)
                errors += "error" in doc
                done += 1
                if progress and (done % 50 == 0 or done == len(todo)):
                    progress("Fetching company statements", done, len(todo))
    return {"due": len(todo), "fetched": done, "errors": errors}


# ----------------------------------------------------------------------------------------------
# Nash quality screen (research/nash_screen.py, nash_screen_clean.py)

def cyclical(sector: str, industry: str) -> bool:
    if sector == "Industrials":
        return industry not in INDUSTRIAL_KEEP
    return sector in CYCLICAL_SECTORS or (sector == "Technology" and industry in CYCLICAL_TECH)


def nash_snapshots(doc: dict) -> list[tuple[str, str, dict]]:
    """(available date, latest quarter end, metrics) for every TTM window with 8 consecutive quarters."""
    inc = sorted((r for r in doc.get("income", []) if r.get("date")), key=lambda r: r["date"])
    bal = {r["date"]: r for r in doc.get("balance", []) if r.get("date")}
    cfs = {r["date"]: r for r in doc.get("cash", []) if r.get("date")}
    out = []
    for k in range(7, len(inc)):
        window = inc[k - 7:k + 1]                      # oldest .. newest
        gaps = [(_d(b["date"]) - _d(a["date"])).days for a, b in zip(window, window[1:])]
        if any(g < 60 or g > 120 for g in gaps):
            continue
        prior, recent = window[:4], window[4:]

        def total(rows, key):
            vals = [r.get(key) for r in rows]
            return None if any(v is None for v in vals) else float(sum(vals))

        rev, rev0 = total(recent, "revenue"), total(prior, "revenue")
        if not rev or not rev0 or rev <= 0 or rev0 <= 0:
            continue
        opx, opx0 = total(recent, "operatingExpenses"), total(prior, "operatingExpenses")
        oi = total(recent, "operatingIncome")
        fcf_rows = [cfs.get(r["date"]) for r in recent]
        fcf = None if any(r is None or r.get("freeCashFlow") is None for r in fcf_rows) else \
            float(sum(r["freeCashFlow"] for r in fcf_rows))
        b = bal.get(window[-1]["date"]) or {}
        cash, debt = b.get("cashAndShortTermInvestments"), b.get("totalDebt")
        leases = b.get("capitalLeaseObligations") or 0
        if debt is not None and 0 < leases <= debt:
            debt = debt - leases
        m = {
            "rev_g": rev / rev0 - 1,
            "opx_g": (opx / opx0 - 1) if opx is not None and opx0 and opx0 > 0 and opx > 0 else None,
            "om": oi / rev if oi is not None else None,
            "fcfm": fcf / rev if fcf is not None else None,
            "cash_gt_debt": (cash > debt) if cash is not None and debt is not None else None,
        }
        m["r40"] = m["rev_g"] + m["fcfm"] if m["fcfm"] is not None else None
        out.append((available_from(window[-1]), window[-1]["date"], m))
    out.sort(key=lambda x: x[0])
    return out


def nash_clean_snapshots(doc: dict) -> list[tuple[str, str, dict]]:
    """nash_snapshots plus the post-hoc data guards: one currency, FCF margin within +-100%, prior-year revenue in USD."""
    inc = sorted((r for r in doc.get("income", []) if r.get("date")), key=lambda r: r["date"])
    out = []
    for avail, qend, m in nash_snapshots(doc):
        k = [r["date"] for r in inc].index(qend)
        window = inc[k - 7:k + 1]
        currencies = {r.get("reportedCurrency") for r in window}
        if len(currencies) != 1 or next(iter(currencies)) not in FX:
            continue
        rate = FX[next(iter(currencies))]
        rev0 = sum(r["revenue"] for r in window[:4]) / rate
        if m["fcfm"] is not None and not -1 <= m["fcfm"] <= 1:
            continue
        out.append((avail, qend, {**m, "rev0_usd": rev0}))
    return out


def latest_snapshot(snaps, day: str, stale_days: int):
    """Most recent snapshot available strictly before ``day`` and not stale, as (quarter end, metrics)."""
    best = None
    for avail, qend, m in snaps:
        if avail < day:
            best = (qend, m)
        else:
            break
    if best is None or (_d(day) - _d(best[0])).days > stale_days:
        return None
    return best


def nash_passes(m: dict, p) -> bool:
    if not m["cash_gt_debt"] or m["rev_g"] < p.min_revenue_growth_pct / 100:
        return False
    margin = m["om"] if p.margin == "OM" else m["fcfm"]
    if margin is None or margin < p.min_margin_pct / 100:
        return False
    return m["opx_g"] is not None and m["rev_g"] > m["opx_g"]


def _pct(value):
    return None if value is None else _num(100 * value, 1)


class NashScanner(Scanner):
    id = "nash_quality"
    needs_api = needs_statements = True
    entry = "close"
    candidate_columns = [("rule_of_40", "Rule of 40, pts"), ("revenue_growth_pct", "Revenue growth, %"),
                         ("fcf_margin_pct", "FCF margin, %"), ("operating_margin_pct", "Operating margin, %"),
                         ("quarter_end", "Latest quarter")]

    def sessions_needed(self, p) -> int:
        return 120

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        last, day = panel.last, (date.fromisoformat(str(panel.dates[-1])) + timedelta(days=1)).isoformat()
        result = ScanResult()
        built = nash_clean_snapshots if p.data_guards else nash_snapshots
        counts = {"liquid": 0, "with_statements": 0, "pass_rules": 0}
        passing = []
        for j in np.flatnonzero(panel.eligible[last]):
            symbol = str(panel.symbols[j])
            info = inp.meta.get(symbol, {})
            sector, industry = info.get("sector") or "", info.get("industry") or ""
            if sector == "Financial Services" or (p.exclude_cyclicals and cyclical(sector, industry)):
                continue
            counts["liquid"] += 1
            doc = inp.statements.get(symbol)
            if not doc:
                continue
            counts["with_statements"] += 1
            snap = latest_snapshot(built(doc), day, p.stale_days)
            if snap is None or not nash_passes(snap[1], p):
                continue
            qend, m = snap
            if p.data_guards and m["rev0_usd"] < p.min_prior_revenue_m * 1e6:
                continue
            passing.append((j, qend, m))
        counts["pass_rules"] = len(passing)
        ranked = sorted(passing, key=lambda x: (-(x[2]["r40"] if x[2]["r40"] is not None else -9), str(panel.symbols[x[0]])))
        for rank, (j, qend, m) in enumerate(ranked, 1):
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[last]), "rank": rank,
                    "order": [rank, str(panel.symbols[j])], "rule_of_40": _pct(m["r40"]),
                    "revenue_growth_pct": _pct(m["rev_g"]), "fcf_margin_pct": _pct(m["fcfm"]),
                    "operating_margin_pct": _pct(m["om"]), "quarter_end": qend, "weight_pct": _num(100 / p.slots, 2)}
            result.candidates.append(item)
        result.members = [c["symbol"] for c in result.candidates]
        result.counts = counts
        result.notes.append("Holdings that still pass every rule are kept at each quarterly rebalance; empty slots take the "
                            "highest Rule of 40 (revenue growth plus free-cash-flow margin).")
        return result


# ----------------------------------------------------------------------------------------------
# Traveling Trader checklist (research/tt_quality.py, strategies/tt_checklist_top10.py)

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
            return np.array([_float(r.get(key)) for r in inc])
        return np.array([_float((by[src].get(d) or {}).get(key)) for d in dates])

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
    m = {"ni_t": ni_t, "roic": roic, "de": de, "pe_med": pe_med, "ni_g": ni_g, "ni_lag_ok": ni_lag_ok.astype(float),
         "fcf_up": fcf_up, "mcap": mcap, "mccy_ok": mc_ok.astype(float)}
    return QSeries(dates=dates, avail=[available_from(r) for r in inc], ccy=ccy, run=run, m=m)


def snapshot_index(s: QSeries, day: str, stale_days: int) -> int | None:
    """Newest quarter usable strictly before ``day``: eight consecutive quarters and not stale."""
    best = None
    for k, a in enumerate(s.avail):
        if a < day:
            best = k if best is None or s.dates[k] > s.dates[best] else best
    if best is None or s.run[best] < 8 or (_d(day) - _d(s.dates[best])).days > stale_days:
        return None
    return best


def quality_flags(s: QSeries, k: int, close_now: float, close_then: float, p) -> dict:
    """The checklist's five tests at quarter index k. 1 passes, 0 fails, NaN cannot be evaluated."""
    m = {name: v[k] for name, v in s.m.items()}
    mcap_t = np.nan
    if m["mccy_ok"] == 1 and m["mcap"] > 0 and np.isfinite(close_then) and close_then > 0:
        mcap_t = m["mcap"] * close_now / close_then
    pe_now = mcap_t / m["ni_t"] if np.isfinite(mcap_t) and m["ni_t"] > 0 else np.nan
    row = {"pe": pe_now, "pe_median": m["pe_med"], "roic": m["roic"], "de": m["de"], "ni_g": m["ni_g"]}
    if np.isfinite(m["pe_med"]) and np.isfinite(mcap_t) and np.isfinite(m["ni_t"]):
        row["PEHIST"] = float(np.isfinite(pe_now) and pe_now < m["pe_med"])
    else:
        row["PEHIST"] = np.nan
    if np.isfinite(mcap_t) and m["ni_lag_ok"] == 1:
        row["PEGT"] = float(np.isfinite(pe_now) and np.isfinite(m["ni_g"]) and m["ni_g"] > 0
                            and pe_now / (100 * m["ni_g"]) <= p.peg_max)
    else:
        row["PEGT"] = np.nan
    row["ROIC15"] = float(m["roic"] >= p.roic_min_pct / 100) if not np.isnan(m["roic"]) else np.nan
    row["DE1"] = float(m["de"] < p.debt_equity_max) if not np.isnan(m["de"]) else np.nan
    row["FCFUP"] = m["fcf_up"]
    row["QUAL"] = combo(row, ("PEHIST", "PEGT", "ROIC15", "DE1", "FCFUP"))
    return row


def combo(row: dict, names: tuple) -> float:
    vals = [row[nm] for nm in names]
    if any(v == 0 for v in vals if not np.isnan(v)):
        return 0.0
    if any(np.isnan(v) for v in vals):
        return np.nan
    return 1.0


def close_on_or_before(panel, j: int, day: str) -> float:
    i = int(np.searchsorted(panel.dates, day, side="right")) - 1
    while i >= 0 and not np.isfinite(panel.close_ff[i, j]):
        i -= 1
    return float(panel.close_ff[i, j]) if i >= 0 else np.nan


def trend_ok(close: np.ndarray, sessions: int) -> bool:
    """Newest close strictly above its trailing simple average; all closes in the window must be valid."""
    window = close[-sessions:]
    return bool(len(window) == sessions and np.isfinite(window).all() and window[-1] > window.mean())


class ChecklistScanner(Scanner):
    id = "tt_checklist"
    needs_api = needs_statements = True
    entry = "close"
    candidate_columns = [("pe", "P/E, trailing"), ("pe_median", "Own median P/E"), ("peg", "PEG, trailing"),
                         ("roic_pct", "ROIC, %"), ("debt_equity", "Debt/equity"), ("stop", "Stop, USD")]

    def sessions_needed(self, p) -> int:
        return 800

    def scan(self, inp):
        panel, p = inp.panel, inp.params
        last, day = panel.last, (date.fromisoformat(str(panel.dates[-1])) + timedelta(days=1)).isoformat()
        result = ScanResult()
        counts = {"liquid": 0, "evaluable": 0, "pass_checklist": 0}
        passers = []
        for j in np.flatnonzero(panel.eligible[last]):
            symbol = str(panel.symbols[j])
            if (inp.meta.get(symbol, {}).get("sector") or "") == "Financial Services":
                continue
            counts["liquid"] += 1
            doc = inp.statements.get(symbol)
            series = quality_series(doc) if doc else None
            k = snapshot_index(series, day, p.stale_days) if series else None
            if k is None or not np.isfinite(panel.ret63[last, j]):
                continue
            row = quality_flags(series, k, float(panel.close[last, j]), close_on_or_before(panel, j, series.dates[k]), p)
            counts["evaluable"] += int(not np.isnan(row["QUAL"]))
            if row["QUAL"] == 1:
                passers.append((j, row))
        counts["pass_checklist"] = len(passers)
        ranked = sorted(passers, key=lambda x: (-float(panel.ret63[last, x[0]]), str(panel.symbols[x[0]])))
        market_ok = True
        if p.trend_gate in ("market", "both"):
            market_ok = trend_ok(inp.spy_close, p.trend_sessions)
        for rank, (j, row) in enumerate(ranked, 1):
            peg = row["pe"] / (100 * row["ni_g"]) if np.isfinite(row["pe"]) and np.isfinite(row["ni_g"]) and row["ni_g"] > 0 else np.nan
            close = float(panel.close[last, j])
            item = {**_identity(panel, j, inp.meta, last), "signal_date": str(panel.dates[last]), "pe": _num(row["pe"], 1),
                    "pe_median": _num(row["pe_median"], 1), "peg": _num(peg, 2), "roic_pct": _num(100 * row["roic"], 1),
                    "debt_equity": _num(row["de"], 2), "stop": _num(close * (1 - p.stop_pct / 100)),
                    "risk_pct": _num(p.stop_pct, 2), "weight_pct": _num(min(p.max_position_pct, 100 / p.top_n), 2)}
            if rank > p.top_n:
                result.skipped.append({**item, "reason": f"Ranked {rank} by 63-session return, outside the top {p.top_n}"})
                continue
            item.update(rank=rank, order=[rank, item["symbol"]])
            result.members.append(item["symbol"])
            blocked = None
            if p.trend_gate in ("stock", "both") and not trend_ok(panel.close[:, j], p.trend_sessions):
                blocked = f"Below its {p.trend_sessions}-day average"
            elif not market_ok:
                blocked = f"SPY is below its {p.trend_sessions}-day average"
            if blocked:
                result.blocked.append({**item, "reason": blocked})
            else:
                result.candidates.append(item)
        result.counts = counts
        result.notes.append("The research rebalances on the first session of each calendar quarter: it buys the new "
                            "selections, sells holdings outside the top selections, and keeps each entry's original stop.")
        return result


scanning.SCANNERS.update({s.id: s for s in (NashScanner(), ChecklistScanner())})
