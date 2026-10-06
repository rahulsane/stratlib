"""The MSCI GARP live screen: what the MSCI USA Quality GARP Select Index holds today, rebuilt from MSCI's rules
(msci_garp.py) on the S&P 500 and the cached statements.

A screen rebuilds the latest review that has taken effect, and the ``warmup_reviews`` before it so that the buffer
for current members applies as the index would apply it. Each review's parent is the S&P 500 on its data date, from
FMP's constituent list and change log. Between reviews the index's shares are fixed: a stock that leaves the S&P 500
leaves the index (its value is spread over the rest) and nothing is added, so today's weights are the review's
weights moved with prices since the weights were set. The screen lists every holding by today's weight, and its
``members`` are what Positions compares holdings with.

The data are the research's caches (research/garp_data.py, research/mscigarp_data.py), refreshed from FMP when a
screen is run online: the S&P 500 list and change log (``research:garp:sp500``, two calls, at most weekly), and for
each company whose statements are missing, older than REFRESH_DAYS or older than an earnings release, five calls:
annual and quarterly income statements, quarterly balance sheets, quarterly cash flow and dividends.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import msci_garp as MG
from . import scanning
from .fmp import FMPAuthError, FMPError
from .scanning import ScanResult, Scanner, _identity, _num

log = logging.getLogger(__name__)

MEMBERS_KEY = "research:garp:sp500"          # shared with research/garp_data.py
STATEMENT_KEY = "research:garp:stmts:"
EXTRA_KEY = "research:mscigarp:extra:"
DIVIDEND_KEY = "research:nash:div:"
MEMBERS_REFRESH_DAYS = 7
FIELDS = {
    "income_annual": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency", "revenue",
                      "netIncome", "eps", "epsDiluted", "weightedAverageShsOut", "weightedAverageShsOutDil",
                      "interestIncome", "interestExpense"),
    "income_quarter": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency", "revenue",
                       "netIncome", "eps", "epsDiluted", "weightedAverageShsOut", "weightedAverageShsOutDil",
                       "interestIncome", "interestExpense"),
    "balance_quarter": ("date", "period", "fiscalYear", "filingDate", "acceptedDate", "reportedCurrency",
                        "totalDebt", "totalStockholdersEquity", "totalEquity", "minorityInterest", "preferredStock"),
}
EXTRA_FIELDS = {
    "balance_quarter": ("date", "filingDate", "acceptedDate", "cashAndCashEquivalents", "cashAndShortTermInvestments",
                        "minorityInterest"),
    "cash_quarter": ("date", "filingDate", "acceptedDate", "operatingCashFlow", "depreciationAndAmortization", "netIncome"),
}


# ----------------------------------------------------------------------------------------------
# S&P 500 membership (research/garp_data.py membership)

@dataclass
class Member:
    ticker: str
    names: set = field(default_factory=set)
    spans: list = field(default_factory=list)        # [start, end) ISO dates; end "" while still a member

    def in_index(self, day: str) -> bool:
        return any(a <= day and (not b or day < b) for a, b in self.spans)


def membership(doc: dict, since: str) -> dict[str, Member]:
    """Each ticker's time in the S&P 500 from ``since``: today's list walked back through the change log. A change
    dated D takes effect before D's open."""
    current = {x["symbol"]: x.get("name") for x in doc["current"]}
    events = sorted(doc["events"], key=lambda e: e["date"])
    members = set(current)
    for e in reversed(events):
        if e["date"] < since:
            break
        a, r = (e.get("symbol") or "").strip(), (e.get("removedTicker") or "").strip()
        if a and a != r:
            members.discard(a)
        if r:
            members.add(r)
    out: dict[str, Member] = {}

    def get(t):
        return out.setdefault(t, Member(t))

    for t in members:
        get(t).spans.append([since, ""])
    for e in events:
        if e["date"] < since:
            continue
        d = e["date"]
        a, r = (e.get("symbol") or "").strip(), (e.get("removedTicker") or "").strip()
        if r:
            m = get(r)
            if e.get("removedSecurity"):
                m.names.add(e["removedSecurity"].strip())
            if m.spans and not m.spans[-1][1]:
                m.spans[-1][1] = d
        if a and a != r:
            m = get(a)
            if e.get("addedSecurity"):
                m.names.add(e["addedSecurity"].strip())
            if not (m.spans and not m.spans[-1][1]):
                m.spans.append([d, ""])
    for t, name in current.items():
        if name:
            get(t).names.add(name)
    return {t: m for t, m in out.items() if m.spans}


def load_members(store, client, today: date) -> dict | None:
    """The cached S&P 500 list and change log, refetched online when older than a week."""
    doc = store.document(MEMBERS_KEY)
    if client is None or (doc and (today - date.fromisoformat(doc.get("fetched_on", "0001-01-01"))).days < MEMBERS_REFRESH_DAYS):
        return doc
    events = client.get("historical-sp500-constituent", {})
    current = client.get("sp500-constituent", {})
    if not isinstance(events, list) or not isinstance(current, list) or not current:
        raise scanning.ScanError("Unexpected S&P 500 constituent response; no result was saved.")
    doc = {"fetched_on": today.isoformat(), "events": events, "current": current}
    store.save_document(MEMBERS_KEY, doc)
    return doc


# ----------------------------------------------------------------------------------------------
# Statements: the research's bundles, refreshed from FMP

def fetch_company(client, symbol: str, today: date) -> dict:
    """Five calls: annual and quarterly income statements, quarterly balance sheets (both bundles read them), quarterly
    cash flow and dividends. Returns the three documents to save."""
    stamp = today.isoformat()
    statements, extra, errors = {"fetched_on": stamp}, {"fetched_on": stamp}, {}

    def rows(path, period, limit):
        try:
            got = client.get(path, {"symbol": symbol, "period": period, "limit": limit})
        except FMPAuthError:
            raise
        except FMPError as exc:
            errors[f"{path}:{period}"] = str(exc)
            return []
        return [r for r in got or [] if isinstance(r, dict)]

    income_annual = rows("income-statement", "annual", 30)
    income_quarter = rows("income-statement", "quarter", 100)
    balance = rows("balance-sheet-statement", "quarter", 100)
    cash = rows("cash-flow-statement", "quarter", 100)
    for kind, found in (("income_annual", income_annual), ("income_quarter", income_quarter), ("balance_quarter", balance)):
        statements[kind] = [{k: r.get(k) for k in FIELDS[kind]} for r in found]
    extra["balance_quarter"] = [{k: r.get(k) for k in EXTRA_FIELDS["balance_quarter"]} for r in balance]
    extra["cash_quarter"] = [{k: r.get(k) for k in EXTRA_FIELDS["cash_quarter"]} for r in cash]
    if errors:
        statements["errors"] = extra["errors"] = errors
    try:
        paid = client.get("dividends", {"symbol": symbol})
        dividends = {"fetched_on": stamp,
                     "rows": {r["date"]: float(r.get("adjDividend") or 0) for r in paid or [] if isinstance(r, dict) and r.get("date")}}
    except FMPAuthError:
        raise
    except FMPError as exc:
        dividends = {"fetched_on": stamp, "rows": {}, "error": str(exc)}
    return {STATEMENT_KEY: statements, EXTRA_KEY: extra, DIVIDEND_KEY: dividends}


def refresh_companies(store, client, symbols: list[str], today: date, *, workers: int = 4, progress=None) -> dict:
    """Refetch the companies whose bundles are due; a failed fetch never replaces a cached bundle."""
    from .fundamental_scans import statements_due
    todo = statements_due(store, client, symbols, today, progress=progress, key=STATEMENT_KEY)
    done = errors = 0
    if todo:
        with ThreadPoolExecutor(max(1, workers)) as pool:
            for symbol, docs in zip(todo, pool.map(lambda s: fetch_company(client, s, today), todo)):
                failed = "errors" in docs[STATEMENT_KEY] or "error" in docs[DIVIDEND_KEY]
                if failed and store.document(STATEMENT_KEY + symbol) is not None:
                    errors += 1
                    continue
                for prefix, doc in docs.items():
                    store.save_document(prefix + symbol, doc)
                errors += failed
                done += 1
                if progress and (done % 25 == 0 or done == len(todo)):
                    progress("Fetching company statements", done, len(todo))
    return {"due": len(todo), "fetched": done, "errors": errors}


# ----------------------------------------------------------------------------------------------
# The index at a review

def one_listing(symbols: list[str], names: dict[str, str], dollar_volume: dict[str, float]) -> list[str]:
    """One share class per company (same name tokens): the one with the larger 20-session dollar volume."""
    groups: dict = {}
    for s in symbols:
        key = MG.tokens(names.get(s)) or frozenset([s])
        if key not in groups or dollar_volume[s] > groups[key][1]:
            groups[key] = (s, dollar_volume[s])
    return sorted(s for s, _ in groups.values())


def dividends_per_share(rows: dict, sessions: np.ndarray, ref: int) -> float:
    """Dividends with an ex-date among the year of sessions ending at ``ref`` (the research's 252 rows)."""
    days = set(map(str, sessions[max(0, ref - 251):ref + 1]))
    return float(sum(v for d, v in rows.items() if d in days and v))


@dataclass
class Inputs:
    """What a review reads besides prices."""
    members: dict                 # ticker -> Member
    funds: dict                   # ticker -> msci_garp.Fund
    extra: dict                   # ticker -> msci_garp.extra_series
    dividends: dict               # ticker -> {ex-date: dividend}
    names: dict
    sectors: dict                 # ticker -> (sector, industry)
    fund: dict | None = None      # the GARP ETF's published holdings: {"as_of", "rows": [[ticker, name, sector, %, price]]}


# The index's actual holdings: what the iShares GARP ETF publishes every day (it holds the index). MSCI's own list is
# free too, but months late and without tickers (estimate_snapshots.py saves it monthly).
FUND_KEY = "research:mscigarp:fund_holdings"
FUND_STALE_DAYS = 5


def fund_rows(fund: dict | None) -> list[tuple]:
    """(ticker, name, sector, weight %, price or None) per holding; files saved before 2026-10-02 have no price."""
    return [(r[0], r[1], r[2], r[3], r[4] if len(r) > 4 else None) for r in (fund or {}).get("rows", [])]


def load_fund(store, online: bool, as_of: str) -> tuple[dict | None, str | None]:
    """(The fund's latest holdings, a warning). An online run downloads and caches them; otherwise, or when the
    download fails, the newest cached copy is used: this cache or a monthly estimates snapshot's."""
    from . import estimate_snapshots as ES
    problem = None
    if online:
        try:
            doc = ES.ishares_holdings(ES.download(ES.ISHARES["garp"]))
            if not doc["rows"]:
                raise ValueError("no equity rows")
            doc["fetched_on"] = date.today().isoformat()
            store.save_document(FUND_KEY, doc)
            return doc, None
        except Exception as exc:  # a changed or unreachable page falls back to the cache
            problem = f"iShares' holdings file could not be downloaded ({type(exc).__name__}: {exc})"
    cached = [d for d in [store.document(FUND_KEY)] if d and d.get("rows")]
    for key in store.document_keys(ES.KEY)[:3]:
        files = (store.document(key) or {}).get("files") or {}
        if (files.get("garp") or {}).get("rows"):
            cached.append(files["garp"])
    if not cached:
        return None, (problem + ", and none is cached." if problem else None)
    doc = max(cached, key=lambda d: d.get("as_of") or "")
    stale = doc.get("as_of") and (date.fromisoformat(as_of) - date.fromisoformat(doc["as_of"])).days > FUND_STALE_DAYS
    notes = [problem] if problem else []
    if stale:
        notes.append(f"{'the' if notes else 'The'} index's holdings are iShares' file of {doc['as_of']}; run the screen "
                     "online to refresh them")
    return doc, "; ".join(notes) + "." if notes else None


def review(panel, last_valid: np.ndarray, inputs: Inputs, reb: dict, current: set[str], p) -> dict:
    """One review: parent, scores, selection and target weights."""
    ref, n = reb["reference"], len(panel.dates)
    ref_day = str(panel.dates[ref])
    parent = [str(s) for j, s in enumerate(panel.symbols)
              if np.isfinite(panel.close[ref, j]) and str(s) in inputs.members and inputs.members[str(s)].in_index(ref_day)]
    lo = max(0, ref - 19)
    with np.errstate(invalid="ignore"):
        dollars = panel.close[lo:ref + 1] * panel.volume[lo:ref + 1]
    traded = {}
    for s in parent:
        column = dollars[:, panel.index[s]]
        traded[s] = float(np.nanmean(column)) if np.isfinite(column).any() else float("nan")
    stocks = []
    for s in one_listing(parent, inputs.names, traded):
        sector, industry = inputs.sectors.get(s, ("", ""))
        fund = inputs.funds.get(s) or MG.Fund([], [], [])
        ex = inputs.extra.get(s) or {"cfo": [], "cash": []}
        stocks.append({"symbol": s, "sector": sector or "Unknown", "industry": industry or "",
                       "price": float(panel.close[ref, panel.index[s]]),
                       "dps": dividends_per_share(inputs.dividends.get(s, {}), panel.dates, ref),
                       "fund": MG.fundamentals_at(fund, ex, reb["cutoff"])})
    sc = MG.score(stocks, p.growth_variant)
    chosen = MG.select(sc, current, p.coverage_pct / 100, p.buffer_low_pct / 100, p.buffer_high_pct / 100)
    after = str(panel.dates[min(reb["effective"] + 1, n - 1)])
    alive = np.array([(inputs.members[sc["symbols"][k]].in_index(after) or reb["effective"] == n - 1)
                      and last_valid[panel.index[sc["symbols"][k]]] >= reb["weights"] for k in chosen], dtype=bool)
    chosen = chosen[alive] if len(chosen) else chosen
    weights = MG.tilt_weights(sc, chosen, p.max_issuer_pct / 100, p.sector_band_pct / 100)
    return {"sc": sc, "chosen": chosen, "weights": weights, "tilt": MG.tilts(sc, chosen),
            "symbols": [sc["symbols"][k] for k in chosen], "coverage": float(sc["pw"][chosen].sum())}


def carried(close: np.ndarray) -> np.ndarray:
    """Closes carried forward without limit, zero before a stock's first bar (the research's px)."""
    px = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    for i in range(1, len(px)):
        miss = np.isnan(px[i])
        px[i, miss] = px[i - 1, miss]
    return np.nan_to_num(px)


# A stock without a bar for this many sessions has stopped trading; a shorter gap is a missing bar.
STOPPED_SESSIONS = 5


class MsciGarpScanner(Scanner):
    id = "msci_garp"
    needs_api = True
    entry = "close"
    keep_all = True
    candidate_columns = [("weight_pct", "Weight in the index, %"), ("rebuild_pct", "Weight in the rebuild, %"),
                         ("growth", "Growth score"), ("value", "Value score"), ("quality", "Quality score"),
                         ("tilt", "Tilt")]

    def sessions_needed(self, p) -> int:
        # The warm-up reviews' data dates, a year of dividends before the first and the weights lag.
        return 63 * (p.warmup_reviews + 1) + 252 + 40

    def load(self, store, client, sessions: list[str], today: date, *, workers: int, warnings: list,
             progress=None) -> tuple[list[str], Inputs]:
        """Every S&P 500 member since the first session and what the reviews read about them, and the index's actual
        holdings."""
        if progress:
            progress("Reading the S&P 500 list", 0, 1)
        doc = load_members(store, client, today)
        if not doc:
            raise scanning.ScanError("The S&P 500 list is not cached. Run the screen once with the statement refresh.")
        members = membership(doc, sessions[0])
        constituents = {x["symbol"]: x for x in doc["current"]}
        symbols = sorted(members)
        if client is not None:
            refreshed = refresh_companies(store, client, symbols, today, workers=workers, progress=progress)
            if refreshed["errors"]:
                warnings.append(f"{refreshed['errors']} companies' statements could not be fetched; their cached "
                                "statements were kept.")
        if progress:
            progress("Reading the index's holdings", 0, 1)
        fund, problem = load_fund(store, client is not None, sessions[-1])
        if problem:
            warnings.append(problem)
        listed = store.sectors()
        universe = {row["symbol"]: row["name"] for row in store.universe()}
        funds, extra, dividends, names, sectors, missing = {}, {}, {}, {}, {}, []
        for s in symbols:
            sector, industry = listed.get(s, ("", ""))
            listing = constituents.get(s) or {}
            sectors[s] = (sector or listing.get("sector") or "", industry or listing.get("subSector") or "")
            names[s] = universe.get(s) or listing.get("name") or next(iter(sorted(members[s].names)), "")
            doc = store.document(STATEMENT_KEY + s)
            if doc is None and members[s].in_index(sessions[-1]):
                missing.append(s)
            funds[s] = MG.fundamentals(doc, sectors[s][0] == "Financial Services") if doc else MG.Fund([], [], [])
            extra[s] = MG.extra_series(store.document(EXTRA_KEY + s))
            dividends[s] = (store.document(DIVIDEND_KEY + s) or {}).get("rows", {})
        for ticker, name, sector, _weight, _price in fund_rows(fund):
            if ticker not in names:                       # held by the index outside the S&P 500
                names[ticker] = universe.get(ticker) or name.title()
                sectors[ticker] = (listed.get(ticker, ("", ""))[0] or sector, listed.get(ticker, ("", ""))[1])
        if missing:
            warnings.append(f"{len(missing)} current S&P 500 members have no cached statements and score as missing "
                            f"({', '.join(missing[:8])}{', ...' if len(missing) > 8 else ''})"
                            + ("." if client else "; run the screen with the statement refresh to fetch them."))
        if client is None:
            warnings.append("Cache-only run: the S&P 500 list, company statements and the index's holdings were not "
                            "refreshed.")
        held = sorted({row[0] for row in fund_rows(fund)} - set(symbols))
        return symbols + held, Inputs(members, funds, extra, dividends, names, sectors, fund)

    def scan(self, inp):
        panel, p, data = inp.panel, inp.params, inp.extra
        last, dates = panel.last, panel.dates
        px = carried(panel.close)
        valid = np.isfinite(panel.close) & (panel.close > 0)
        last_valid = np.where(valid.any(axis=0), len(dates) - 1 - valid[::-1].argmax(axis=0), -1)
        reviews = [r for r in MG.schedule(dates, int(str(dates[0])[:4])) if r["reference"] >= 251]
        if not reviews:
            raise scanning.ScanError("Not enough cached history for an MSCI GARP review. Run stratlib backfill.")
        reviews = reviews[-(p.warmup_reviews + 1):]
        current: set[str] = set()
        for reb in reviews:
            found = review(panel, last_valid, data, reb, current, p)
            current = set(found["symbols"])
        reb, sc = reviews[-1], found["sc"]
        eff, weights_row = reb["effective"], reb["weights"]
        meta = {s: {"name": data.names.get(s), "sector": data.sectors.get(s, ("", ""))[0],
                    "industry": data.sectors.get(s, ("", ""))[1]} for s in data.names}
        shares = np.zeros(len(panel.symbols))
        cols = np.array([panel.index[s] for s in found["symbols"]], dtype=int)
        shares[cols] = found["weights"] / px[weights_row, cols]
        result = ScanResult()
        left = []
        for i in range(eff + 1, last):                  # between reviews the index only loses stocks
            gone = []
            for j in np.flatnonzero(shares > 0):
                member = data.members[str(panel.symbols[j])]
                if (member.in_index(str(dates[i])) and not member.in_index(str(dates[i + 1]))
                        or (last_valid[j] == i and last - i >= STOPPED_SESSIONS)):
                    gone.append(j)
            if gone:
                total = float(shares @ px[i])
                for j in gone:
                    left.append({**_identity(panel, j, meta, last), "signal_date": str(dates[i]),
                                 "weight_pct": _num(100 * shares[j] * px[i, j] / total, 3),
                                 "reason": f"Left the rebuilt index at the close of {dates[i]}: it left the S&P 500 or "
                                           "stopped trading"})
                    shares[j] = 0.0
                shares *= total / float(shares @ px[i])
        value = shares * px[last]
        rebuilt = {str(panel.symbols[j]): 100 * value[j] / value.sum() for j in np.flatnonzero(shares > 0)}
        tilt = dict(zip(found["symbols"], found["tilt"]))
        place = {s: k for k, s in enumerate(sc["symbols"])}

        def scores(s):
            k = place.get(s)
            if k is None:
                return {"growth": None, "value": None, "quality": None, "tilt": None}
            return {"growth": _num(sc["growth"][k], 2), "value": _num(sc["value"][k], 2),
                    "quality": _num(sc["quality"][k], 2), "tilt": _num(tilt[s], 2) if s in tilt else None}

        upcoming = MG.next_review(str(dates[last]))
        summary = {"review": reb["label"], "effective": str(dates[eff]), "data_date": reb["cutoff"],
                   "weights_date": str(dates[weights_row]), "next_review": upcoming, "warmup_reviews": len(reviews) - 1}
        fund = data.fund
        if fund:
            rows = fund_rows(fund)
            total = sum(r[3] for r in rows) or 1.0
            for ticker, name, sector, weight, price in rows:
                j = panel.index.get(ticker)
                if j is not None and np.isfinite(panel.close[last, j]):
                    item = _identity(panel, j, meta, last)
                else:                                   # no stored prices (another share class, a new listing)
                    item = {"symbol": ticker, "name": data.names.get(ticker) or name.title(),
                            "sector": data.sectors.get(ticker, (sector, ""))[0] or sector,
                            "industry": data.sectors.get(ticker, ("", ""))[1], "close": _num(price), "ret63_pct": None}
                result.candidates.append({**item, "weight_pct": _num(100 * weight / total, 3),
                                          "rebuild_pct": _num(rebuilt.get(ticker, 0.0), 3), **scores(ticker)})
            index_weights = {c["symbol"]: c["weight_pct"] for c in result.candidates}
            both = set(index_weights) & set(rebuilt)
            result.counts = {"index_held": len(rows), "rebuild_held": len(rebuilt), "both": len(both),
                             "rebuild_overlap_pct": _num(sum(min(index_weights[s], rebuilt[s]) for s in both), 1)}
            result.summary = {**summary, "source": "ishares", "as_of": fund.get("as_of")}
            result.notes.append(
                f"The holdings are the MSCI USA Quality GARP Select Index as the iShares GARP ETF held it on "
                f"{fund.get('as_of')}, from the file iShares publishes every day. The scores, tilts and rebuild weights come "
                f"from this app's rebuild of MSCI's rules (its {reb['label']} review), which holds {len(rebuilt)} stocks "
                f"and matches {result.counts['rebuild_overlap_pct']}% of the index's weight; stocks outside the S&P 500 "
                f"have no scores. The next review takes effect at the close of the last session on or before {upcoming}.")
        else:
            place_now = {s: w for s, w in rebuilt.items()}
            for s, w in place_now.items():
                result.candidates.append({**_identity(panel, panel.index[s], meta, last), "weight_pct": _num(w, 3),
                                          "rebuild_pct": _num(w, 3), **scores(s)})
            result.skipped = left
            result.counts = {"parent": sc["universe"], "selected": len(found["symbols"]),
                             "coverage_pct": _num(100 * found["coverage"], 1), "held": len(result.candidates)}
            result.summary = {**summary, "source": "rebuild"}
            result.notes.append(
                f"The index's published holdings were not available, so these are the holdings of this app's rebuild of "
                f"MSCI's rules: its {reb['label']} review, which took effect at the close of {dates[eff]} on data as of "
                f"{reb['cutoff']}, with weights set at the close of {dates[weights_row]} and moved with prices since. The "
                f"next review takes effect at the close of the last session on or before {upcoming}.")
        result.candidates.sort(key=lambda c: (-(c["weight_pct"] or 0), c["symbol"]))
        for rank, c in enumerate(result.candidates, 1):
            c.update(rank=rank, order=[rank, c["symbol"]])
        result.members = [c["symbol"] for c in result.candidates]
        return result


scanning.SCANNERS[MsciGarpScanner.id] = MsciGarpScanner()
