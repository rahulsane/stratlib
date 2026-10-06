"""Approximate backtest: today's FMP histories, each statement dated by filing.

This is the method canslim_prompt.md describes. A statement becomes public on
its SEC acceptance or filing date (45/90-day fallback). FMP's delisted-company
directory restores stocks that failed or were acquired, and every filter is
applied as of each session. It cannot undo later restatements or industry
reclassifications, so every result is labelled approximate. The strict
archive engine in backtest_data is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .backfill import sync_prices
from .backtest import BacktestError, Signal, simulate, valid_bar
from .backtest_data import ET, public_statements, refresh_delistings, restore_price_basis, statement_index
from .bases import detect_base, history_weeks
from .config import BacktestSettings, Settings, Thresholds
from .fmp import FMPAuthError, FMPError, FMPPlanRestrictedError
from .market_direction import CORRECTION, market_direction
from .market_stats import industry_ranks, rs_ratings
from .parallel import run_tasks
from .prices import Bar, eod_cutoff
from .scoring import (CRITERIA, leadership_criteria, meets_rule, number, score_fundamentals, split_factor,
                      trend_order)
from .technical import buyable, technical_criteria
from .universe import company_key, exclusion_reason, sample_symbols

MODE = "approximate"
PREPARED = "backtest:approx:prepared"
SPLITS = "backtest:approx:splits"
PROFILE = "backtest:approx:profile:"
STATEMENTS = "backtest:approx:statements:"
# Statement kinds the scorer reads. How deep to fetch depends on the period.
STATEMENT_CALLS = (("income_quarter", "income_statement", "quarter"),
                   ("income_annual", "income_statement", "annual"),
                   ("balance_annual", "balance_sheet", "annual"),
                   ("cash_annual", "cash_flow", "annual"))


# Sessions per worker task in a backtest's daily screens; fewer would spend more on start-up reads than they save.
DAY_BATCH_MIN = 5

# The statement fields the C/A/S/L scorer reads; the rest are dropped to save space.
STATEMENT_FIELDS = frozenset({
    "date", "fiscalYear", "period", "reportedCurrency", "acceptedDate", "filingDate", "fillingDate",
    "epsDiluted", "netIncome", "netIncomeFromContinuingOperations", "netIncomeFromDiscontinuedOperations",
    "otherAdjustmentsToNetIncome", "weightedAverageShsOutDil", "revenue", "totalDebt",
    "totalStockholdersEquity", "operatingCashFlow",
})


def slim_rows(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k in STATEMENT_FIELDS} for row in rows]


class StatementCache:
    """Statement histories by symbol, trimmed to scorer fields, keeping the most recently used. Each history's
    filing timestamps are read once, for every session that asks what was public."""

    def __init__(self, store, size: int = 2000):
        from collections import OrderedDict
        self.store, self.size, self.docs, self.fetched, self.indexes = store, size, OrderedDict(), {}, {}

    def get(self, symbol: str) -> dict | None:
        if symbol in self.docs:
            self.docs.move_to_end(symbol)
            return self.docs[symbol]
        doc = self.store.document(STATEMENTS + symbol)
        if doc and "error" not in doc:
            doc = {k: slim_rows(v) if k in {c[0] for c in STATEMENT_CALLS} else v for k, v in doc.items()}
        self.docs[symbol] = doc
        self.fetched[symbol] = (doc or {}).get("fetched_on")
        if len(self.docs) > self.size:
            dropped, _ = self.docs.popitem(last=False)
            self.indexes.pop(dropped, None)
        return doc

    def public(self, symbol: str, day: str) -> dict:
        """The statements public at this session's cutoff; call only for a symbol with a usable history."""
        doc = self.get(symbol)
        if symbol not in self.indexes:
            self.indexes[symbol] = statement_index(doc)
        return public_statements(doc, day, self.indexes[symbol])


def statement_depth(start: str, today: str | None = None) -> dict:
    """Quarters and years to fetch so the first test day still has 12 quarters
    (margins) and four fiscal years (three annual growth rates) behind it."""
    months = max(0, (date.fromisoformat(today or _today()) - date.fromisoformat(start)).days) / 30.44
    return {"quarter": 12 + int(months // 3) + 3, "annual": 4 + int(months // 12) + 2}
SPLIT_CHUNK_DAYS = 60   # FMP silently truncates longer calendar ranges to their latest ~90 days
LIMITATIONS = [
    "Approximate: statement histories were downloaded today and dated by SEC acceptance or filing date "
    "(45 days after quarter end or 90 after year end when neither exists). Later restatements can make "
    "past numbers look cleaner than investors saw them.",
    "Industries and sectors are today's classifications, including for stocks that delisted.",
    "Delisted companies come from the delisted-company directory; their profiles screen out ETFs and funds. A held stock that "
    "delisted is sold at its last close unless documented proceeds were imported.",
    "Days with no bar for a listed stock drop it from that day's ranking; a holding is marked at its last close "
    "through such a gap and cannot be bought on a carried-forward price.",
    "I sponsorship is manual and is not an automated historical entry filter. Today's manual verdicts are never reused.",
    "Fractional shares; each new position receives equity / maximum holdings at that open. No rebalancing of existing lots.",
    "No commissions, slippage, taxes, cash interest or dividends. SPY is a price-return benchmark, bought at the first open.",
]


def warmup(t: Thresholds) -> int:
    return max(t.high_sessions, t.long_ma_sessions, t.short_ma_sessions, t.volume_sessions,
               t.rs_quarter_sessions * 4 + 1, t.industry_sessions + 1, t.distribution_window_sessions + 1)


def _today() -> str:
    return datetime.now(timezone.utc).astimezone(ET).date().isoformat()


def default_period(store, settings: Settings, now: datetime | None = None) -> tuple[date, date]:
    """The most recent cached year: the day after the same date a year before the last SPY session."""
    cutoff = eod_cutoff(now or datetime.now(timezone.utc), settings.prices.eod_final_hour_et)
    spy = store.price_history("SPY", limit=1, through=cutoff.isoformat())
    end = date.fromisoformat(spy[-1].date) if spy else cutoff
    return end - timedelta(days=365 * settings.backtest.default_years - 1), end


def check(store, settings: Settings, start: str, end: str) -> dict:
    """Cheap preflight: SPY calendar, warm-up history and prepared statements."""
    if date.fromisoformat(start) >= date.fromisoformat(end):
        raise BacktestError("Start date must precede end date.")
    if end > eod_cutoff(datetime.now(timezone.utc), settings.prices.eod_final_hour_et).isoformat():
        raise BacktestError("End date must be a completed daily session or earlier.")
    t = settings.thresholds
    all_sessions = [b.date for b in store.price_history("SPY", through=end)]
    days = [d for d in all_sessions if start <= d <= end]
    errors = []
    if len(days) < 2:
        errors.append("Need at least two cached SPY sessions within this period.")
    elif bisect_right(all_sessions, days[0]) < warmup(t):
        errors.append(f"Need {warmup(t)} cached SPY sessions before the first test date. Run the price backfill.")
    elif all_sessions[-1] < end and (date.fromisoformat(end) - date.fromisoformat(all_sessions[-1])).days > 3:
        errors.append("SPY history does not reach the requested end. Run the price backfill.")
    prepared = store.document(PREPARED)
    if not prepared or prepared["start"] > start or prepared["end"] < end:
        errors.append("Prepare data for this period first. It downloads delisted companies, splits and "
                      "statement histories.")
    return {"ready": not errors, "errors": errors, "sessions": len(days),
            "first_session": days[0] if days else None, "last_session": days[-1] if days else None,
            "prepared": {k: v for k, v in prepared.items() if k != "survivors"} if prepared else None}


# ----------------------------------------------------------------------
# Historical members


def delisted_candidates(store, settings: Settings, start: str, end: str) -> list[dict]:
    """Directory rows listed at some point in the period, one per ticker."""
    delist = store.document("backtest:delisted")
    if not delist:
        return []
    chosen = {}
    for row in delist["rows"]:
        try:
            first, last, symbol = date.fromisoformat(row["ipoDate"]), date.fromisoformat(row["delistedDate"]), row["symbol"]
        except (ValueError, KeyError, TypeError):
            continue
        if (row.get("exchange") not in settings.universe.exchanges or first > last
                or first.isoformat() > end or last.isoformat() <= start):
            continue
        if exclusion_reason(symbol, row.get("companyName"), exchange=row["exchange"], is_etf=False, is_fund=False):
            continue
        if symbol not in chosen or row["delistedDate"] > chosen[symbol]["delistedDate"]:
            chosen[symbol] = row
    return [chosen[s] for s in sorted(chosen)]


def members(store, settings: Settings, start: str, end: str) -> tuple[dict[str, dict], Counter]:
    """Today's common stocks plus delisted common stocks listed during the period."""
    counts = Counter()
    result = {row["symbol"]: {"name": row["name"], "exchange": row["exchange"], "industry": row["industry"],
                              "until": None, "source": "current"}
              for row in store.universe() if row["exchange"] in settings.universe.exchanges}
    counts["current_members"] = len(result)
    companies = {company_key(m["name"]) for m in result.values()} - {None}
    for row in delisted_candidates(store, settings, start, end):
        symbol = row["symbol"]
        if symbol in result:
            # The ticker now belongs to a listed company; its prices are that company's.
            counts["delisted_tickers_reused"] += 1
            continue
        key = company_key(row.get("companyName"))
        if key in companies:
            # An old ticker, another class or a note of a company already present.
            counts["delisted_same_company"] += 1
            continue
        profile = (store.document(PROFILE + symbol) or {}).get("profile")
        if profile and exclusion_reason(symbol, row.get("companyName"), exchange=row["exchange"],
                                        is_etf=bool(profile.get("isEtf")), is_fund=bool(profile.get("isFund"))):
            counts["delisted_non_common"] += 1
            continue
        counts["delisted_members" if profile else "delisted_members_without_profile"] += 1
        if key:
            companies.add(key)
        result[symbol] = {"name": row.get("companyName"), "exchange": row["exchange"],
                          "industry": (profile or {}).get("industry") or None, "until": row["delistedDate"],
                          "source": "delisted"}
    return result, counts


# ----------------------------------------------------------------------
# Daily ranking, computed for the whole market at once


def split_rows(store) -> dict[str, list[dict]]:
    doc = store.document(SPLITS) or {"rows": []}
    rows = {}
    for row in doc["rows"]:
        numerator, denominator = number(row.get("numerator")), number(row.get("denominator"))
        try:
            date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if numerator and denominator and numerator > 0 and denominator > 0 and row.get("symbol"):
            rows.setdefault(row["symbol"], []).append(row)
    return rows


def rankings(store, settings: Settings, start: str, end: str, member_map: dict[str, dict], *, progress=None,
             chunk_days: int = 252, detail_days: set[str] | None = None, workers: int = 1) -> dict:
    """Price filter, RS rating and industry rank for every member on every test session.

    Matches market_stats.price_metrics: every window needs a complete run of
    valid closes, and the absolute price and volume checks use each day's
    pre-split basis. Members with no bar that day are left out of the ranking.
    Sessions are processed in chunks of chunk_days, each with its own warm-up
    rows, so long periods need little memory.

    detail_days ranks only those sessions and adds each rated member's
    filter inputs under "features", for research.

    The chunks are independent, so ``workers`` processes can rank them at once (see parallel.py). The chunk
    size stays fixed either way: each chunk's warm-up decides when a stock first appears in it.
    """
    t = settings.thresholds
    all_sessions = [b.date for b in store.price_history("SPY", through=end)]
    days = [d for d in all_sessions if d >= start]
    symbols = sorted(member_map)
    splits = split_rows(store)
    states = store.price_states(symbols)
    basis = {s: states[s].checked_at.astimezone(ET).date() for s in symbols if s in states and states[s].checked_at}
    chunks = []
    for c in range(0, len(days), chunk_days):
        part = days[c:c + chunk_days]
        if detail_days is not None:
            part = [d for d in part if d in detail_days]
            if not part:
                continue
        rows = all_sessions[max(0, bisect_left(all_sessions, part[0]) - warmup(t) - 5):
                            bisect_right(all_sessions, part[-1])]
        active = [s for s in symbols if member_map[s]["until"] is None or member_map[s]["until"] > part[0]]
        chunks.append((rows, part, active, (c, len(days))))
    context = {"t": t, "member_map": member_map, "splits": splits, "basis": basis, "detail": detail_days is not None}
    result, counts, priced = {}, Counter(), set()
    for chunk_result, chunk_counts, chunk_priced in run_tasks(
            store, context, _rank_chunk, chunks, workers=workers, local={"progress": progress},
            progress=progress and (lambda done, total: progress("Ranking the market", min(done * chunk_days, len(days)),
                                                                len(days)))):
        result.update(chunk_result)
        counts.update(chunk_counts)
        priced |= chunk_priced
    counts["members_without_prices"] = len(set(symbols) - priced)
    return {"days": result, "all_sessions": all_sessions, "counts": dict(counts)}


def _rank_chunk(state: dict, chunk: tuple) -> tuple[dict, Counter, set]:
    """One chunk's rankings, gap count and priced members."""
    store, t, member_map, splits, basis, detail = (state[k] for k in ("store", "t", "member_map", "splits", "basis",
                                                                       "detail"))
    rows, part, symbols, position = chunk
    progress = state.get("progress")
    result, counts, priced = {}, Counter(), set()
    index = {d: i for i, d in enumerate(rows)}
    shape = (len(rows), len(symbols))
    close, high, volume = np.full(shape, np.nan), np.full(shape, np.nan), np.full(shape, np.nan)
    for j, symbol in enumerate(symbols):
        for day, h, c, v in store.price_rows(symbol, rows[0], rows[-1]):
            i = index.get(day)
            if i is not None:
                close[i, j] = c
                high[i, j] = h if h is not None else np.nan
                volume[i, j] = v if v is not None else np.nan
        if progress and j % 1000 == 0:
            progress(f"Loading prices for {part[0][:4]} {j:,}/{len(symbols):,}", position[0], position[1])
    with np.errstate(invalid="ignore"):
        valid = np.isfinite(close) & (close > 0)
        high_ok = np.isfinite(high) & (high > 0)
        volume_ok = np.isfinite(volume) & (volume >= 0)
    closes = pd.DataFrame(np.where(valid, close, np.nan))

    def complete(mask, n):
        return pd.DataFrame(mask.astype(float)).rolling(n).sum().to_numpy() == n

    def average(n):
        return np.where(complete(valid, n), closes.rolling(n).mean().to_numpy(), np.nan)

    def lagged(n):
        out = np.full(shape, np.nan)
        out[n:] = close[:-n]
        return out

    ma_short, ma_long = average(t.short_ma_sessions), average(t.long_ma_sessions)
    highs = np.where(complete(valid, t.high_sessions) & complete(high_ok, t.high_sessions),
                     pd.DataFrame(np.where(high_ok, high, np.nan)).rolling(t.high_sessions).max().to_numpy(), np.nan)
    avg_volume = np.where(complete(valid, t.volume_sessions) & complete(volume_ok, t.volume_sessions),
                          pd.DataFrame(np.where(volume_ok, volume, np.nan)).rolling(t.volume_sessions).mean().to_numpy(),
                          np.nan)
    q = t.rs_quarter_sessions
    anchors = [close] + [lagged(q * k) for k in range(1, 5)]
    with np.errstate(divide="ignore", invalid="ignore"):
        quarters = [anchors[k] / anchors[k + 1] - 1 for k in range(4)]
        rs_score = np.where(complete(valid, 4 * q + 1),
                            100 * (t.rs_recent_weight * quarters[0] + sum(quarters[1:])) / (t.rs_recent_weight + 3), np.nan)
        industry_return = np.where(complete(valid, t.industry_sessions + 1),
                                   100 * (close / lagged(t.industry_sessions) - 1), np.nan)
        below = 100 * (1 - close / highs)
    # A stock joins the ranking at its first bar in the window (an IPO), and leaves at delisting.
    first_bar = np.where(valid.any(axis=0), valid.argmax(axis=0), len(rows))
    priced.update(symbols[j] for j in np.flatnonzero(first_bar < len(rows)))
    for day in part:
        i = index[day]
        listed = [j for j, s in enumerate(symbols) if first_bar[j] <= i
                  and (member_map[s]["until"] is None or day < member_map[s]["until"])]
        present = [j for j in listed if valid[i, j]]
        counts["member_sessions_with_price_gaps"] += len(listed) - len(present)
        scores = {symbols[j]: float(rs_score[i, j]) for j in present if np.isfinite(rs_score[i, j])}
        ratings = rs_ratings(scores)
        groups = industry_ranks({symbols[j]: member_map[symbols[j]]["industry"] for j in present},
                                {symbols[j]: float(industry_return[i, j]) for j in present
                                 if np.isfinite(industry_return[i, j])})
        survivors, features, traded = {}, {}, {}
        for j in present:
            symbol = symbols[j]
            if ratings.get(symbol, 0) < t.rs_min and not (detail and symbol in ratings):
                continue
            factor = 1.0
            if symbol in splits and symbol in basis:
                factor = split_factor(splits[symbol], date.fromisoformat(day), basis[symbol]) or 1.0
            checks = (close[i, j] * factor >= t.min_price, avg_volume[i, j] / factor >= t.min_avg_volume,
                      below[i, j] <= t.max_below_high_pct, close[i, j] > ma_short[i, j], close[i, j] > ma_long[i, j])
            if ratings[symbol] >= t.rs_min and all(bool(c) for c in checks):
                survivors[symbol] = {"rs": ratings[symbol], "industry": member_map[symbol]["industry"]}
                traded[symbol] = float(close[i, j] * avg_volume[i, j])
            if detail:
                features[symbol] = {"rs": ratings[symbol], "industry": member_map[symbol]["industry"],
                                    "price": float(close[i, j] * factor), "avg_volume": float(avg_volume[i, j] / factor),
                                    "below_high_pct": float(below[i, j]), "above_short_ma": bool(checks[3]),
                                    "above_long_ma": bool(checks[4]), "short_ma_gap_pct": float(100 * (close[i, j] / ma_short[i, j] - 1)),
                                    "price_pass": all(bool(c) for c in checks)}
        result[day] = {"members": len(present), "ranked": len(ratings), "survivors": survivors, "groups": groups,
                       "dollar_volume": traded}
        if detail:
            result[day]["features"] = features
        if progress:
            progress(f"Ranking {day}", position[0] + part.index(day) + 1, position[1])
    return result, counts, priced


# ----------------------------------------------------------------------
# Data preparation (FMP calls)


def _parallel(items, work, workers: int, progress=None, label: str = ""):
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, item) for item in items]
        for future in as_completed(futures):
            future.result()
            done += 1
            if progress and (done % 25 == 0 or done == len(items)):
                progress(f"{label} {done:,}/{len(items):,}", done, len(items))


def refresh_split_calendar(client, store, start: str, symbols=(), *, workers: int = 1, progress=None) -> dict:
    """Every split from start to today.

    FMP's calendar reaches back only a plan-dependent window (about five years
    on Premium). Splits before that come from each symbol's own history, which
    is stored under the strict mode's key and never refetched: past splits
    do not change.
    """
    today = _today()
    cached = store.document(SPLITS)
    if cached and cached["fetched_on"] == today and cached["from"] <= start:
        return cached
    rows, first = {}, date.fromisoformat(start)
    day = covered = date.fromisoformat(today)
    # Walk back from today, so the plan limit costs one refused call.
    while day >= first:
        begin = max(day - timedelta(days=SPLIT_CHUNK_DAYS - 1), first)
        try:
            batch = client.splits_calendar(begin, day, cache_ttl=None)
        except FMPPlanRestrictedError:
            break
        if not isinstance(batch, list):
            raise BacktestError("Unexpected splits-calendar response.")
        if batch and min(r.get("date", "") for r in batch) > (begin + timedelta(days=7)).isoformat():
            raise BacktestError("A splits-calendar range came back truncated; shorten SPLIT_CHUNK_DAYS.")
        for row in batch:
            rows[(row.get("symbol"), row.get("date"))] = row
        covered, day = begin, begin - timedelta(days=1)
    unavailable = []
    if covered > first:
        states = store.price_states(symbols)
        older = [s for s in symbols if s in states and (states[s].first_date or "9999") < covered.isoformat()]

        def history(symbol):
            doc = store.document(f"backtest:splits:{symbol}")
            if doc and doc["fetched_on"] >= covered.isoformat():
                return
            try:
                found = client.splits(symbol, cache_ttl=None)
            except FMPAuthError:
                raise
            except FMPError:
                found = None
            if not isinstance(found, list) or split_factor(found, date.min, date.max) is None:
                store.save_document(f"backtest:splits:{symbol}", {"fetched_on": "", "rows": [], "error": True})
            else:
                store.save_document(f"backtest:splits:{symbol}", {"fetched_on": today, "rows": found})

        _parallel(older, history, workers, progress, "Split histories")
        for symbol in older:
            doc = store.document(f"backtest:splits:{symbol}")
            if doc.get("error"):
                unavailable.append(symbol)
            for row in doc["rows"]:
                if start <= str(row.get("date", "")) < covered.isoformat():
                    rows[(symbol, row["date"])] = {**row, "symbol": symbol}
    doc = {"fetched_on": today, "from": start, "to": today, "calendar_from": covered.isoformat(),
           "rows": list(rows.values()), "histories_unavailable": unavailable}
    store.save_document(SPLITS, doc)
    return doc


def fetch_statements(client, store, symbol: str, splits: list[dict], depth: dict) -> None:
    today = _today()
    doc = {"fetched_on": today, "share_basis_date": today, "splits": splits, "depth": depth,
           "balance_quarter": [], "cash_quarter": []}
    try:
        for key, method, period in STATEMENT_CALLS:
            rows = getattr(client, method)(symbol, period, depth[period], cache_ttl=timedelta(days=1))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise BacktestError(f"{symbol}: unexpected {key} response")
            doc[key] = slim_rows(rows)
    except FMPAuthError:
        raise
    except (FMPError, BacktestError) as exc:
        doc = {"fetched_on": today, "error": str(exc)}
    store.save_document(STATEMENTS + symbol, doc)


def prepare(client, store, settings: Settings, start: str, end: str, *, progress=None,
            now: datetime | None = None) -> dict:
    """Fetch everything an approximate run needs. Safe to repeat; cached pieces are reused."""
    now = now or datetime.now(timezone.utc)
    note = progress or (lambda message, done, total: None)
    initial, workers = client.stats.api_calls, settings.fmp.max_workers
    # Ranking warm-up and base detection read history_weeks before the first test day.
    needed = date.fromisoformat(start) - timedelta(weeks=history_weeks(settings.thresholds) + 2)
    years = max(settings.prices.history_years, math.ceil((now.date() - needed).days / 365.25))
    longer = replace(settings.prices, history_years=years)

    def extend(symbols, label):
        states = store.price_states(symbols)
        missing = [s for s in symbols if s not in states or (states[s].requested_from or "9999") > needed.isoformat()]
        if missing:
            sync_prices(client, store, missing, longer, now=now, workers=workers,
                        progress=lambda done, total, stats: note(f"{label} {done:,}/{total:,}", done, total))

    # The index histories come first: the period check needs their warm-up sessions.
    extend(list(settings.prices.market_symbols), "Index price histories")
    status = check(store, settings, start, end)
    blocking = [e for e in status["errors"] if not e.startswith("Prepare data")]
    if blocking:
        raise BacktestError("\n".join(blocking))
    delist = store.document("backtest:delisted")
    if not delist or delist.get("complete") is not True or delist.get("fetched_at", "")[:10] < end:
        refresh_delistings(client, store, progress=lambda message, calls: note(message, 0, 0))
    current = {row["symbol"] for row in store.universe()}
    candidates = [r["symbol"] for r in delisted_candidates(store, settings, start, end) if r["symbol"] not in current]

    def profile(symbol):
        try:
            rows = client.profile(symbol, cache_ttl=None)
        except FMPAuthError:
            raise
        except FMPError:
            rows = []
        found = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        store.save_document(PROFILE + symbol, {"fetched_on": _today(), "profile": found})

    _parallel([s for s in candidates if store.document(PROFILE + s) is None], profile, workers, note, "Delisted profiles")
    member_map, member_counts = members(store, settings, start, end)
    delisted = [s for s, m in member_map.items() if m["source"] == "delisted"]
    extend(sorted(member_map), "Price histories")
    splits = refresh_split_calendar(client, store, start, sorted(member_map), workers=workers, progress=note)
    ranked = rankings(store, settings, start, end, member_map, progress=note)
    survivors = sorted({s for day in ranked["days"].values() for s in day["survivors"]})
    by_symbol = {}
    for row in splits["rows"]:
        by_symbol.setdefault(row.get("symbol"), []).append(row)
    depth = statement_depth(start)

    def outdated(symbol):
        doc = store.document(STATEMENTS + symbol) or {}
        # Histories fetched before depth was recorded used 20 quarters and 6 years.
        have = doc.get("depth", {"quarter": 20, "annual": 6})
        return (doc.get("fetched_on", "") < end or have["quarter"] < depth["quarter"]
                or have["annual"] < depth["annual"])

    stale = [s for s in survivors if outdated(s)]
    _parallel(stale, lambda s: fetch_statements(client, store, s, by_symbol.get(s, []), depth), workers, note,
              "Statement histories")
    failed = [s for s in survivors if "error" in (store.document(STATEMENTS + s) or {"error": ""})]
    summary = {"start": start, "end": end, "prepared_at": now.isoformat(),
               "api_calls": client.stats.api_calls - initial, "members": len(member_map),
               "delisted_members": len(delisted), "survivors": survivors, "survivor_count": len(survivors),
               "statements_unavailable": len(failed), "split_rows": len(splits["rows"]), "statement_depth": depth,
               "split_histories_unavailable": len(splits.get("histories_unavailable", [])),
               "price_thresholds": _price_thresholds(settings.thresholds), **member_counts}
    store.save_document(PREPARED, summary)
    return {k: v for k, v in summary.items() if k != "survivors"}


def _price_thresholds(t: Thresholds) -> dict:
    keys = ("min_price", "min_avg_volume", "max_below_high_pct", "high_sessions", "short_ma_sessions",
            "long_ma_sessions", "volume_sessions", "rs_quarter_sessions", "rs_recent_weight", "rs_min")
    return {k: getattr(t, k) for k in keys}


# ----------------------------------------------------------------------
# Run


def execution_bars(store, symbol: str, days: list[str], until: str | None, end: str):
    """Real bars plus flat carried-forward marks for gaps inside the listing."""
    real = {b.date: b for b in store.price_history(symbol, through=end) if valid_bar(b)}
    bars, stale, last = [], set(), None
    for day in days:
        if until is not None and day >= until:
            break
        if day in real:
            last = real[day]
            bars.append(last)
        elif last is not None:
            bars.append(Bar(day, last.close, last.close, last.close, last.close, 0.0))
            stale.add(day)
    settlement = None
    if until is not None and until <= days[-1]:
        before = [b for d, b in real.items() if d < until]
        settlement = max(before, key=lambda b: b.date).close if before else None
    return bars, stale, settlement


def run(store, settings: Settings, start: str, end: str, *, portfolio: BacktestSettings | None = None,
        sample: int | None = None, progress=None, label: str | None = None, workers: int = 1) -> dict:
    """The breakout strategy on the approximate method's data. ``workers`` processes share the ranking and the
    daily screens (see parallel.py before raising it from a script); the result is the same either way."""
    status = check(store, settings, start, end)
    if not status["ready"]:
        raise BacktestError("\n".join(status["errors"]))
    if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int) or sample < 1):
        raise BacktestError("Development sample must be a positive integer.")
    t, portfolio = settings.thresholds, portfolio or settings.backtest
    note = progress or (lambda message, done, total: None)
    market_histories = {s: store.price_history(s, through=end) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}
    market = market_direction(market_histories, t, as_of=end)
    states = {row["date"]: row["state"] for row in market["history"]}
    exposures = {row["date"]: row["exposure"] for row in market["history"]}
    member_map, member_counts = members(store, settings, start, end)
    ranked = rankings(store, settings, start, end, member_map, progress=note, workers=workers)
    days = list(ranked["days"])
    if any(states.get(day) is None for day in days):
        raise BacktestError("Market direction has missing index/volume evidence in the selected period.")
    corrections = sorted(row["date"] for row in market["history"] if row["state"] == CORRECTION)
    splits = split_rows(store)
    basis = {s: p.checked_at.astimezone(ET).date().isoformat()
             for s, p in store.price_states(member_map).items() if p.checked_at}
    funnel = ("candidate_sessions", "survivor_sessions_without_statements", "unavailable_survivor_sessions",
              "fundamental_pass_sessions", "buyable_sessions", "breakouts", "breakouts_blocked_by_market", "signals")
    counts, diagnostics = Counter({**dict.fromkeys(funnel, 0), **ranked["counts"]}), []
    statements = StatementCache(store)
    prepared = store.document(PREPARED)
    if prepared["price_thresholds"] != _price_thresholds(t):
        counts["price_thresholds_changed_since_prepare"] = 1

    # Every close but the last is screened, so the funnel shows what the market gate blocked; only a confirmed
    # uptrend's close schedules the next open.
    screens = _daily_screens(store, settings, ranked, member_map, splits, basis, states, exposures, corrections,
                             end=end, sample=sample, max_holdings=portfolio.max_holdings, statements=statements,
                             workers=workers, note=note)

    signals, blocked, breakouts = {}, [], set()
    rejections = {key: Counter() for key, _, _ in CRITERIA}
    for index, day in enumerate(days):
        info = ranked["days"][day]
        counts["price_rs_survivor_sessions"] += len(info["survivors"])
        found = []
        if index < len(days) - 1:
            screen = screens[day]
            counts.update(screen["counts"])
            for key, tally in screen["rejections"].items():
                rejections.setdefault(key, Counter()).update(tally)
            breakouts.update(screen["breakouts"])
            blocked.extend(screen["blocked"])
            found = signals[day] = screen["signals"]
        counts["signals"] += len(found)
        diagnostics.append({"date": day, "members": info["members"], "ranked": info["ranked"],
                            "survivors": len(info["survivors"]), "signals": len(found), "market": states[day],
                            "exposure": exposures[day]})
    counts["breakouts_blocked_by_market"] = len(blocked)
    counts["breakouts"] = len(breakouts)

    traded = sorted({s.symbol for found in signals.values() for s in found})
    execution, stale, settlements, delisted, approximated = {}, {}, {}, {}, set()
    documented = {(r["symbol"], r["date"]): r for r in (store.document("backtest:settlements") or {}).get("rows", [])}
    for symbol in traded:
        until = member_map[symbol]["until"]
        execution[symbol], stale[symbol], last_close = execution_bars(store, symbol, days, until, end)
        counts["carried_forward_marks"] += len(stale[symbol])
        if until is not None and until <= days[-1]:
            delisted[symbol] = until
            row = documented.get((symbol, until))
            if row and row["basis_date"] == basis.get(symbol):
                settlements[symbol, until] = row["cash_per_share"]
            elif last_close is not None:
                settlements[symbol, until] = last_close
                approximated.add(symbol)
    report = simulate(days, {**execution, "SPY": market_histories["SPY"]}, states, lambda day: signals.get(day, []),
                      t, portfolio, settlements=settlements, delisted=delisted, stale=stale, exposure=exposures,
                      progress=lambda day, done, total: note(f"Trading {day}", done, total))
    for trade in report["trades"]:
        if trade["reason"] == "Delisting settlement" and trade["symbol"] in approximated:
            trade["reason"] = "Delisted; sold at last close (approximation)"
            counts["delisting_exits_at_last_close"] += 1
    report.update(mode=MODE, label=label or None, created_at=datetime.now(timezone.utc).isoformat(), api_calls=0, sample=sample,
                  limitations=LIMITATIONS, signal_counts=dict(counts), daily_screen=diagnostics,
                  criterion_rejections={key: dict(c) for key, c in rejections.items()}, blocked_breakouts=blocked,
                  market_days=dict(Counter(states[day] for day in days)),
                  exposure_days={str(k): v for k, v in sorted(Counter(exposures[day] for day in days).items())},
                  coverage={**{k: v for k, v in status.items() if k != "prepared"}, **member_counts,
                            "members": len(member_map), "statements_prepared": prepared["survivor_count"],
                            "statements_unavailable": prepared["statements_unavailable"],
                            "prepared_at": prepared["prepared_at"]},
                  market_proxy_sessions={s: sum(d["volume_source"] == info["proxy"] for d in info["history"]
                                                if days[0] <= d["date"] <= days[-1]) for s, info in market["indexes"].items()})
    report["input_digest"] = hashlib.sha256(json.dumps(
        {"members": member_map, "prepared": prepared, "thresholds": asdict(t), "portfolio": asdict(portfolio),
         "statements": statements.fetched, "splits": (store.document(SPLITS) or {}).get("fetched_on"), "sample": sample},
        sort_keys=True, default=str).encode()).hexdigest()
    store.save_document("latest_backtest", report)
    store.save_document(f"backtest:result:{report['created_at']}", report)
    return report


def _daily_screens(store, settings: Settings, ranked: dict, member_map: dict, splits: dict, basis: dict, states: dict,
                   exposures: dict, corrections: list[str], *, end: str, sample: int | None, max_holdings: int,
                   statements: StatementCache, workers: int, note) -> dict[str, dict]:
    """Each session's breakout screen, every close but the last. The screens are independent, so batches of
    consecutive sessions can run in separate processes; they come back in session order."""
    days = list(ranked["days"])
    screened = days[:-1]
    size = max(DAY_BATCH_MIN, math.ceil(len(screened) / (2 * max(workers, 1))))
    batches = [[(day, ranked["days"][day]) for day in screened[i:i + size]] for i in range(0, len(screened), size)]
    context = {"t": settings.thresholds, "end": end, "sample": sample, "seed": settings.prices.sample_seed,
               "member_map": member_map, "splits": splits, "basis": basis, "states": states, "exposures": exposures,
               "all_sessions": ranked["all_sessions"], "corrections": corrections, "max_holdings": max_holdings}
    screens = {}
    for batch in run_tasks(store, context, _screen_breakouts, batches, workers=workers, local={"statements": statements},
                           progress=lambda done, total: note("Screening sessions", min(done * size, len(screened)),
                                                             len(screened))):
        screens.update(batch["screens"])
        statements.fetched.update(batch["fetched"])
    return screens


def breakout_signals(store, settings: Settings, start: str, end: str, *, workers: int = 1, progress=None) -> dict:
    """Every session's buyable CANSLIM breakouts, before the market's exposure limits any. The engine backtests
    (stratlib.sim) trade these and apply the exposure ladder themselves. {day: [Signal]}, every close but the last."""
    status = check(store, settings, start, end)
    if not status["ready"]:
        raise BacktestError("\n".join(status["errors"]))
    t, note = settings.thresholds, progress or (lambda message, done, total: None)
    market = market_direction({s: store.price_history(s, through=end) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}, t,
                              as_of=end)
    states = {row["date"]: row["state"] for row in market["history"]}
    member_map, _ = members(store, settings, start, end)
    ranked = rankings(store, settings, start, end, member_map, progress=note, workers=workers)
    if any(states.get(day) is None for day in ranked["days"]):
        raise BacktestError("Market direction has missing index/volume evidence in the selected period.")
    corrections = sorted(row["date"] for row in market["history"] if row["state"] == CORRECTION)
    basis = {s: p.checked_at.astimezone(ET).date().isoformat()
             for s, p in store.price_states(member_map).items() if p.checked_at}
    # Full exposure on every session, so no breakout is held back here.
    screens = _daily_screens(store, settings, ranked, member_map, split_rows(store), basis, states,
                             dict.fromkeys(states, 100.0), corrections, end=end, sample=None,
                             max_holdings=max(settings.backtest.max_holdings, 1), statements=StatementCache(store),
                             workers=workers, note=note)
    return {day: screen["signals"] for day, screen in screens.items()}


def _bars_since(day: str, all_sessions: list[str], t: Thresholds) -> str:
    """The first bar a session's base search reads: its weekly history, or the warm-up if that reaches further."""
    sessions = all_sessions[:bisect_right(all_sessions, day)]
    cutoff = (date.fromisoformat(day) - timedelta(weeks=history_weeks(t) + 1)).isoformat()
    return min(cutoff, sessions[max(0, len(sessions) - warmup(t))])


def _screen_breakouts(state: dict, batch: list[tuple[str, dict]]) -> dict:
    """Consecutive sessions' breakout screens: each price and RS survivor's C/A/S/L checks, then its base.

    Counts, rejections, breakouts, market-blocked entries and signals are kept per session, for the caller to
    merge in session order.
    """
    store, t, end = state["store"], state["t"], state["end"]
    statements = state.setdefault("statements", StatementCache(store))
    member_map, splits, basis = state["member_map"], state["splits"], state["basis"]
    states, exposures = state["states"], state["exposures"]
    all_sessions, corrections = state["all_sessions"], state["corrections"]
    # A later session's bars start no earlier than the first session's, so one read per stock covers the batch.
    since, through, histories = _bars_since(batch[0][0], all_sessions, t), batch[-1][0], {}
    screens = {}
    for day, info in batch:
        counts, rejections, breakouts, blocked, found = Counter(), {}, [], [], []
        survivors = sorted(info["survivors"])
        if state["sample"]:
            survivors = sample_symbols(survivors, state["sample"], state["seed"])
        sessions = all_sessions[:bisect_right(all_sessions, day)]
        seen_corrections = frozenset(corrections[:bisect_right(corrections, day)])
        slots = int(state["max_holdings"] * (exposures.get(day) or 0) / 100 + 1e-9)
        for symbol in survivors:
            counts["candidate_sessions"] += 1
            doc = statements.get(symbol)
            if not doc or "error" in doc:
                counts["survivor_sessions_without_statements"] += 1
                continue
            rs, industry = info["survivors"][symbol]["rs"], info["survivors"][symbol]["industry"]
            criteria, warnings = score_fundamentals(statements.public(symbol, day), t, date.fromisoformat(day))
            criteria += leadership_criteria(rs, info["groups"].get(industry), t)
            missed = [c.key for c in criteria if c.passed is not True]
            for c in criteria:
                if c.passed is not True:
                    rejections.setdefault(c.key, Counter())["failed" if c.passed is False else "unavailable"] += 1
            if len(missed) == 1:
                rejections[missed[0]]["only_blocker"] += 1
            if any(c.passed is None for c in criteria):
                counts["unavailable_survivor_sessions"] += 1
            if not meets_rule(criteria, t):
                continue
            counts["fundamental_pass_sessions"] += 1
            if symbol not in histories:
                histories[symbol] = store.price_history(symbol, since=since, through=through)
            cutoff = _bars_since(day, all_sessions, t)
            recent = [b for b in histories[symbol] if cutoff <= b.date <= day]
            adjusted, factor = restore_price_basis(recent, splits.get(symbol, []), basis.get(symbol, end), day)
            base = detect_base(adjusted, day, t, correction_dates=seen_corrections, sessions=sessions)
            technical = technical_criteria(base, {"state": states[day], "exposure": exposures[day], "as_of": day},
                                           t, day)
            if not buyable(base, day, t):
                continue
            counts["buyable_sessions"] += 1
            breakouts.append((symbol, base["breakout_date"]))
            if slots == 0:
                blocked.append({"symbol": symbol, "date": day, "market": states[day], "exposure": exposures[day],
                                "pattern": base["pattern"], "pivot": base["pivot"]})
                continue
            evidence = {"criteria": [asdict(c) for c in criteria + technical], "base": base,
                        "warnings": warnings, "historical_price_factor": factor,
                        "statements_fetched_on": doc["fetched_on"], "listing": member_map[symbol]["source"]}
            found.append(Signal(symbol, day, base["pivot"] / factor, rs, base["pattern"], evidence,
                                base["breakout_date"]))
        screens[day] = {"counts": counts, "rejections": rejections, "breakouts": breakouts, "blocked": blocked,
                        "signals": found}
    return {"screens": screens, "fetched": dict(statements.fetched)}


LEADERS_LIMITATIONS = [
    "Leaders portfolio: holds the top-ranked stocks that pass the Screen (price and RS filter plus the C/A/S/L "
    "rule), ranked by RS and then by scored checks passed. No base or breakout is required.",
    "Equal weight, one slot per maximum holding; the market's allowed exposure sets how many slots may be filled. "
    "Rebalances sell holdings that no longer pass or rank below the buffer; open slots fill at the next open.",
    "The stop loss applies to each holding; there is no profit target. A stopped-out stock waits for the next "
    "rebalance before it can be bought again.",
]


TREND_LIMITATIONS = [
    "Trend leaders: the rules the nine-year rule study supported. Price and RS leaders (the Screen's price, volume, "
    "high and moving-average filters) with RS of at least backtest.trend_min_rs, an industry group in the top "
    "thresholds.industry_top and latest quarterly sales growth of at least backtest.trend_min_sales_growth_pct, "
    "ranked by RS and then sales growth. The C/A/S/L rule, bases and breakouts are not required.",
    "Equal weight, one slot per maximum holding. The market's allowed exposure limits how many slots may be filled "
    "by new buys; it never forces a sale. Open slots fill at the next open.",
    "A holding is sold only at the open after a close below its long moving average, by the loss cap "
    "(backtest.trend_loss_cap_pct), or at delisting. There is no profit target and no rebalance selling.",
]
STRATEGIES = ("leaders", "trend")
# Thresholds that only market direction reads (market_direction.py), so a leader ranking serves every value of
# them. The distribution window is not one: it also sets each ranking chunk's warm-up.
MARKET_ONLY = frozenset({
    "distribution_decline_pct", "distribution_pressure_count", "distribution_correction_count", "distribution_heavy_count",
    "distribution_expiry_gain_pct", "follow_through_gain_pct", "follow_through_min_day", "market_volume_stale_sessions",
    "market_index_rule", "correction_drawdown_pct", "exposure_confirmed_pct", "exposure_late_confirmed_pct",
    "exposure_new_uptrend_pct", "exposure_new_uptrend_sessions", "exposure_pressure_pct", "exposure_heavy_pressure_pct",
})
ORDERS_CACHE = "backtest:orders:"   # saved leader rankings, newest first; only the most recent are kept
ORDERS_KEPT = 6


def _selection(strategy: str, portfolio: BacktestSettings, t: Thresholds) -> dict:
    """The settings a ranking depends on beyond the thresholds, so a mismatch is caught."""
    if strategy == "trend":
        return {"min_rs": portfolio.trend_min_rs, "min_sales_growth_pct": portfolio.trend_min_sales_growth_pct,
                "min_dollar_volume_m": portfolio.trend_min_dollar_volume_m, "industry_top": t.industry_top}
    return {}


def below_line(store, symbol: str, sessions: int, first: str, end: str) -> set[str]:
    """Sessions from first to end whose close was below the symbol's simple moving average."""
    from .sell_rules import below_trend_line
    bars = [b for b in store.price_history(symbol, through=end) if valid_bar(b)]
    closes = pd.Series([b.close for b in bars], dtype=float)
    line = closes.rolling(sessions).mean()
    return {b.date for b, close, level in zip(bars, closes, line)
            if b.date >= first and math.isfinite(level) and below_trend_line(close, level)}


def leader_orders(store, settings: Settings, start: str, end: str, *, strategy: str = "leaders",
                  portfolio: BacktestSettings | None = None, progress=None, workers: int = 1) -> dict:
    """The expensive part of the leaders portfolio: each close's ranked list of Screen passers.

    Independent of the market rule and the stop loss, so one result serves every variant.
    strategy "trend" lists trend leaders instead (TREND_LIMITATIONS). ``workers`` processes share the work (see
    parallel.py before raising it from a script); the result is the same either way.
    """
    if strategy not in STRATEGIES:
        raise BacktestError(f"Unknown leaders strategy: {strategy}")
    t, portfolio = settings.thresholds, portfolio or settings.backtest
    note = progress or (lambda message, done, total: None)
    member_map, member_counts = members(store, settings, start, end)
    ranked = rankings(store, settings, start, end, member_map, progress=note, workers=workers)
    days = list(ranked["days"])
    counts, orders, diagnostics = Counter(candidate_sessions=0, eligible_sessions=0), {}, []
    statements = StatementCache(store)
    # The last close is not ranked: nothing trades after it. Batches of sessions run independently.
    ranked_days = days[:-1]
    size = max(DAY_BATCH_MIN, math.ceil(len(ranked_days) / (2 * max(workers, 1))))
    batches = [[(day, ranked["days"][day]) for day in ranked_days[i:i + size]]
               for i in range(0, len(ranked_days), size)]
    lists = {}
    for batch in run_tasks(store, {"t": t, "portfolio": portfolio, "strategy": strategy}, _leader_lists, batches,
                           workers=workers, local={"statements": statements},
                           progress=lambda done, total: note("Ranking leaders", min(done * size, len(ranked_days)),
                                                             len(ranked_days))):
        lists.update(batch["lists"])
        statements.fetched.update(batch["fetched"])
    for day in days:
        info, eligible = ranked["days"][day], []
        if day in lists:
            counts.update(lists[day]["counts"])
            eligible = lists[day]["eligible"]
        orders[day] = [symbol for _, symbol in sorted(eligible)]
        counts["eligible_sessions"] += len(eligible)
        diagnostics.append({"date": day, "members": info["members"], "survivors": len(info["survivors"]),
                            "eligible": len(eligible)})
    counts["average_eligible"] = round(counts["eligible_sessions"] / max(len(days) - 1, 1), 1)
    return {"member_map": member_map, "member_counts": member_counts, "days": days, "orders": orders,
            "counts": dict(counts), "diagnostics": diagnostics, "statements": statements.fetched,
            "strategy": strategy, "selection": _selection(strategy, portfolio, t)}


def saved_leader_orders(store, settings: Settings, start: str, end: str, *, strategy: str,
                        portfolio: BacktestSettings, progress=None, workers: int = 1) -> dict:
    """leader_orders, reused from an earlier run whose inputs match exactly: the strategy, the period, every
    threshold the ranking reads, the selection settings and the stored data. Variants that change only the
    portfolio or the market rule then skip the ranking; any backfill or preparation forces a new one."""
    t = settings.thresholds
    key = ORDERS_CACHE + hashlib.sha256(json.dumps({
        "strategy": strategy, "start": start, "end": end, "selection": _selection(strategy, portfolio, t),
        "thresholds": {k: v for k, v in asdict(t).items() if k not in MARKET_ONLY},
        "universe": asdict(settings.universe), "data": store.data_fingerprint(),
    }, sort_keys=True, default=str).encode()).hexdigest()
    saved = store.document(key)
    if saved:
        return saved
    orders = leader_orders(store, settings, start, end, strategy=strategy, portfolio=portfolio, progress=progress,
                           workers=workers)
    store.save_document(key, orders)
    for old in store.document_keys(ORDERS_CACHE)[ORDERS_KEPT:]:
        store.delete_document(old)
    return orders


def _leader_lists(state: dict, batch: list[tuple[str, dict]]) -> dict:
    """Consecutive sessions' eligible leaders, as (order key, symbol), with each session's counts."""
    from .scoring import leader_order
    t, portfolio, strategy = state["t"], state["portfolio"], state["strategy"]
    statements = state.setdefault("statements", StatementCache(state["store"]))
    lists = {}
    for day, info in batch:
        counts, eligible = Counter(), []
        for symbol, row in info["survivors"].items():
            counts["candidate_sessions"] += 1
            doc = statements.get(symbol)
            if not doc or "error" in doc:
                counts["survivor_sessions_without_statements"] += 1
                continue
            criteria, _ = score_fundamentals(statements.public(symbol, day), t, date.fromisoformat(day))
            criteria += leadership_criteria(row["rs"], info["groups"].get(row["industry"]), t)
            if strategy == "trend":
                key, missed = trend_order(symbol, row["rs"], info["groups"].get(row["industry"]), criteria,
                                          info["dollar_volume"].get(symbol), t, portfolio)
                if key is None:
                    counts[missed] += 1
                else:
                    eligible.append((key, symbol))
            elif meets_rule(criteria, t):
                eligible.append((leader_order(row["rs"], criteria, symbol), symbol))
        lists[day] = {"counts": counts, "eligible": eligible}
    return {"lists": lists, "fetched": dict(statements.fetched)}


def run_leaders(store, settings: Settings, start: str, end: str, *, portfolio: BacktestSettings | None = None,
                progress=None, label: str | None = None, orders: dict | None = None,
                strategy: str = "leaders", workers: int = 1) -> dict:
    """The leaders portfolio, or trend leaders, on the approximate method's data. No API calls.

    Pass ``orders`` from leader_orders() to reuse the ranking across variants; otherwise a saved ranking with
    the same inputs is reused (saved_leader_orders).
    """
    from .backtest import simulate_leaders
    status = check(store, settings, start, end)
    if not status["ready"]:
        raise BacktestError("\n".join(status["errors"]))
    t, portfolio = settings.thresholds, portfolio or settings.backtest
    note = progress or (lambda message, done, total: None)
    orders = orders or saved_leader_orders(store, settings, start, end, strategy=strategy, portfolio=portfolio,
                                           progress=note, workers=workers)
    if orders.get("strategy", "leaders") != strategy or orders.get("selection", {}) != _selection(strategy, portfolio, t):
        raise BacktestError("These rankings were made for a different strategy or selection settings.")
    member_map, member_counts, days = orders["member_map"], orders["member_counts"], orders["days"]
    market_histories = {s: store.price_history(s, through=end) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}
    market = market_direction(market_histories, t, as_of=end)
    states = {row["date"]: row["state"] for row in market["history"]}
    exposures = {row["date"]: row["exposure"] for row in market["history"]}
    if any(states.get(day) is None for day in days):
        raise BacktestError("Market direction has missing index/volume evidence in the selected period.")
    counts = Counter(orders["counts"])
    reach = portfolio.leaders_rank_buffer + portfolio.max_holdings
    held = sorted({s for order in orders["orders"].values() for s in order[:reach]})
    execution, stale, settlements, delisted, approximated = {}, {}, {}, {}, set()
    basis = {s: p.checked_at.astimezone(ET).date().isoformat()
             for s, p in store.price_states(held).items() if p.checked_at}
    documented = {(r["symbol"], r["date"]): r for r in (store.document("backtest:settlements") or {}).get("rows", [])}
    for symbol in held:
        until = member_map[symbol]["until"]
        execution[symbol], stale[symbol], last_close = execution_bars(store, symbol, days, until, end)
        if until is not None and until <= days[-1]:
            delisted[symbol] = until
            row = documented.get((symbol, until))
            if row and row["basis_date"] == basis.get(symbol):
                settlements[symbol, until] = row["cash_per_share"]
            elif last_close is not None:
                settlements[symbol, until] = last_close
                approximated.add(symbol)
    # Only symbols with prices loaded can be bought; deeper ranks never reach an open slot.
    ranking = orders["orders"]
    exit_below = ({symbol: below_line(store, symbol, t.long_ma_sessions, days[0], end) for symbol in held}
                  if strategy == "trend" else None)
    report = simulate_leaders(days, {**execution, "SPY": market_histories["SPY"]}, states,
                              lambda day: ranking.get(day, [])[:reach], t, portfolio,
                              exposure=exposures, settlements=settlements, delisted=delisted, stale=stale,
                              exit_below=exit_below,
                              progress=lambda day, done, total: note(f"Trading {day}", done, total))
    for trade in report["trades"]:
        if trade["reason"] == "Delisting settlement" and trade["symbol"] in approximated:
            trade["reason"] = "Delisted; sold at last close (approximation)"
            counts["delisting_exits_at_last_close"] += 1
    prepared = store.document(PREPARED)
    diagnostics = [{**d, "market": states[d["date"]], "exposure": exposures[d["date"]]} for d in orders["diagnostics"]]
    report.update(mode=MODE, strategy=strategy, label=label or None, created_at=datetime.now(timezone.utc).isoformat(),
                  api_calls=0, sample=None, signal_counts=dict(counts),
                  limitations=(TREND_LIMITATIONS if strategy == "trend" else LEADERS_LIMITATIONS) + LIMITATIONS,
                  daily_screen=diagnostics, market_days=dict(Counter(states[day] for day in days)),
                  exposure_days={str(k): v for k, v in sorted(Counter(exposures[day] for day in days).items())},
                  coverage={**{k: v for k, v in status.items() if k != "prepared"}, **member_counts,
                            "members": len(member_map), "statements_prepared": prepared["survivor_count"],
                            "statements_unavailable": prepared["statements_unavailable"],
                            "prepared_at": prepared["prepared_at"]},
                  market_proxy_sessions={s: sum(d["volume_source"] == info["proxy"] for d in info["history"]
                                                if days[0] <= d["date"] <= days[-1]) for s, info in market["indexes"].items()})
    report["input_digest"] = hashlib.sha256(json.dumps(
        {"members": member_map, "prepared": prepared, "thresholds": asdict(t), "portfolio": asdict(portfolio),
         "statements": orders["statements"], "strategy": strategy}, sort_keys=True, default=str).encode()).hexdigest()
    store.save_document("latest_backtest", report)
    store.save_document(f"backtest:result:{report['created_at']}", report)
    return report
