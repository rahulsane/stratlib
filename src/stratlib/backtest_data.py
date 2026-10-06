"""Dated research inputs, FMP delisting coverage, and historical signal generation.

No present-day symbol or industry table is consulted to select past stocks.
Missing historical observations stop a run, rather than backdating current data.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .backtest import BacktestError, Signal, simulate, valid_bar
from .bases import detect_base, history_weeks
from .config import BacktestSettings, Settings, Thresholds
from .market_direction import CONFIRMED, CORRECTION, market_direction
from .market_stats import industry_ranks, price_metrics, rs_ratings
from .prices import Bar, eod_cutoff
from .scoring import leadership_criteria, meets_rule, public_date, score_fundamentals, split_factor
from .technical import buyable, technical_criteria
from .universe import exclusion_reason, parse_listings, sample_symbols

ET = ZoneInfo("America/New_York")
STATEMENT_KEYS = ("income_quarter", "income_annual", "balance_quarter", "balance_annual", "cash_quarter", "cash_annual")
LIMITATIONS = [
    "I sponsorship is manual and is not an automated historical entry filter. Today's manual verdicts are never reused.",
    "Fractional shares; each new position receives equity / maximum holdings at that open. No rebalancing of existing lots.",
    "No commissions, slippage, taxes, cash interest or dividends. SPY is a price-return benchmark, bought at the first open.",
    "Trade prices and shares use the cached split-adjusted basis. Absolute screening prices and volumes are restored to their signal-date basis.",
    "Fundamentals use archived observations plus filing timestamps. No earlier statement vintage is inferred from today's restated statements.",
    "Delisting settlements must be documented on the same price basis. A missing held-position bar or settlement stops the run.",
]


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise BacktestError("Archive timestamps require an explicit time zone.") from None


def session_close(day: str) -> datetime:
    # The existing SPY calendar has no early-close times. A 13:00 cutoff on
    # every date is conservative on regular sessions and safe on half days.
    return datetime.combine(date.fromisoformat(day), time(13), ET).astimezone(timezone.utc)


def statement_available_at(row: dict) -> datetime:
    """SEC timestamps are Eastern when no offset is supplied.

    Date-only filings become usable the following day, avoiding same-day
    after-hours look-ahead. Missing filing/acceptance dates use the specified
    45/90-day fallback, at the beginning of that day.
    """
    for key in ("acceptedDate", "filingDate", "fillingDate"):
        value = str(row.get(key) or "")
        try:
            if len(value) <= 10:
                day = date.fromisoformat(value) + timedelta(days=1)
                return datetime.combine(day, time.min, ET).astimezone(timezone.utc)
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (moment if moment.tzinfo else moment.replace(tzinfo=ET)).astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.combine(public_date(row), time.min, ET).astimezone(timezone.utc)


def available_bundle(vintages: list[dict], day: str) -> dict | None:
    cutoff = session_close(day)
    eligible = [v for v in vintages if timestamp(v["observed_at"]) <= cutoff]
    if not eligible:
        return None
    return public_statements(max(eligible, key=lambda v: timestamp(v["observed_at"]))["body"]["bundle"], day)


def statement_index(bundle: dict) -> dict[str, list[tuple[datetime, str, dict]]]:
    """Each statement kind's rows with the moment they became public and their period date, newest public first.

    Rows whose timestamps or dates cannot be read are left out, as public_statements always does. The sort is
    stable, so rows public at the same moment keep their order.
    """
    index = {}
    for key in STATEMENT_KEYS:
        rows = []
        for row in bundle.get(key, []):
            try:
                rows.append((statement_available_at(row), str(row["date"]), row))
            except (ValueError, KeyError, TypeError):
                continue
        rows.sort(key=lambda item: item[0], reverse=True)
        index[key] = rows
    return index


def public_statements(bundle: dict, day: str, index: dict | None = None) -> dict:
    """Statement rows public by this session's cutoff, newest first.

    Reading the filing timestamps is most of the work, so a caller that asks about the same bundle on many days
    passes statement_index(bundle) once.
    """
    cutoff = session_close(day)
    index = statement_index(bundle) if index is None else index
    result = {**bundle}
    for key in STATEMENT_KEYS:
        result[key] = [row for available, dated, row in index[key] if available <= cutoff and dated <= day]
    return result


def restore_price_basis(bars: list[Bar], splits: list[dict], basis_date: str, day: str) -> tuple[list[Bar], float]:
    """Undo only splits after the signal date that are embedded in the cache.

    Future split records are used to undo provider normalization, never to
    predict a corporate action or select a security.
    """
    factor = split_factor(splits, date.fromisoformat(day), date.fromisoformat(basis_date))
    if factor is None or factor <= 0:
        raise BacktestError("Invalid split history; cannot restore historical prices.")
    return [replace(b, open=b.open * factor if b.open is not None else None,
                    high=b.high * factor if b.high is not None else None,
                    low=b.low * factor if b.low is not None else None, close=b.close * factor,
                    volume=b.volume / factor if b.volume is not None else None)
            for b in bars if b.date <= day], factor


def import_archive(store, payload: dict, *, now: datetime | None = None) -> dict:
    """Import user-supplied dated observations, preserving their provenance.

    This validates structure and chronology, not the truth of the source's
    completeness assertion. It never manufactures historical observations.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise BacktestError("Expected a research archive with schema_version: 1.")
    records = []
    for kind, key in (("universe", "universes"), ("fundamentals", "fundamentals")):
        for item in payload.get(key, []):
            observed = timestamp(item["observed_at"])
            if observed > now:
                raise BacktestError("An archive observation cannot be dated in the future.")
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                raise BacktestError("Every archive observation needs a source description.")
            if kind == "universe":
                day = date.fromisoformat(item["date"]).isoformat()
                if observed.astimezone(ET).date().isoformat() != day or observed > session_close(day):
                    raise BacktestError("Universe observations must be from that session, by 13:00 Eastern.")
                if item.get("complete") is not True or not item.get("stocks"):
                    raise BacktestError("Universe snapshots must declare complete: true and contain all listed securities.")
                symbols = []
                for stock in item["stocks"]:
                    symbols.append(stock["symbol"])
                    if (not isinstance(stock["symbol"], str) or not stock["symbol"].strip()
                            or not isinstance(stock.get("isEtf"), bool) or not isinstance(stock.get("isFund"), bool)
                            or not stock.get("exchange") or "industry" not in stock):
                        raise BacktestError("Snapshot stocks need symbol, exchange, industry, isEtf and isFund.")
                if len(symbols) != len(set(symbols)):
                    raise BacktestError("Duplicate ticker in a universe snapshot; resolve the security identity first.")
                records.append({"kind": kind, "item": day, "observed_at": observed.isoformat(),
                                "body": {"source": source, "complete": True, "stocks": item["stocks"]}})
            else:
                bundle = item["bundle"]
                if (not item.get("symbol") or not all(isinstance(bundle.get(k), list) for k in (*STATEMENT_KEYS, "splits"))
                        or date.fromisoformat(bundle["share_basis_date"]) > observed.astimezone(ET).date()):
                    raise BacktestError("Statement vintages need six statement lists, splits and a historical share_basis_date.")
                records.append({"kind": kind, "item": item["symbol"], "observed_at": observed.isoformat(),
                                "body": {"source": source, "bundle": bundle}})
    if not records:
        raise BacktestError("Archive contains no universe or statement observations.")
    # Optional documented delisting proceeds; no implied liquidation at last quote.
    settlements = payload.get("settlements", [])
    normalized = []
    for item in settlements:
        value = item.get("cash_per_share")
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value < float("inf")
                or not item.get("source") or not item.get("symbol")):
            raise BacktestError("Settlements need symbol, date, nonnegative cash_per_share, basis_date and source.")
        date.fromisoformat(item["date"])
        date.fromisoformat(item["basis_date"])
        normalized.append(item)
    store.save_vintages(records)
    if normalized:
        previous = (store.document("backtest:settlements") or {}).get("rows", [])
        combined = {(row["symbol"], row["date"]): row for row in [*previous, *normalized]}
        store.save_document("backtest:settlements", {"rows": list(combined.values())})
    return {"observations": len(records), "settlements": len(normalized)}


def refresh_delistings(client, store, *, progress=None) -> dict:
    """Read every page; no early stop on dates because FMP does not promise sort order."""
    rows, seen_pages = [], set()
    initial_calls = client.stats.api_calls
    for page in range(1000):
        batch = client.delisted_companies(page=page, limit=100)
        if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
            raise BacktestError("Unexpected delisted-companies response.")
        if not batch:
            result = {"complete": True, "rows": rows, "fetched_at": datetime.now(timezone.utc).isoformat(),
                      "pages": page + 1, "api_calls": client.stats.api_calls - initial_calls}
            store.save_document("backtest:delisted", result)
            return result
        signature = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
        if signature in seen_pages:
            raise BacktestError("The delisting directory repeated a page; coverage is incomplete.")
        seen_pages.add(signature)
        rows.extend(batch)
        if progress:
            progress(f"Delisted companies, page {page + 1}", client.stats.api_calls - initial_calls)
    raise BacktestError("Delisting pagination limit reached; coverage is incomplete.")


def capture_universe(client, store, settings: Settings, *, now: datetime | None = None) -> dict:
    """Capture fresh, unfiltered FMP metadata today. Never relabel it as a past date."""
    fixed_now = now
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ET).date().isoformat()
    if now > session_close(day) or now.astimezone(ET).weekday() > 4:
        raise BacktestError("Capture the universe on a trading morning before 13:00 Eastern.")
    rows = []
    for exchange in settings.universe.exchanges:
        for page in range(50):
            batch = client.company_screener(exchange=exchange, include_all_share_classes=True, page=page,
                                           is_etf=None, is_fund=None, is_actively_trading=None,
                                           limit=settings.universe.screener_page_size, cache_ttl=None)
            if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
                raise BacktestError("Unexpected company-screener response; no snapshot saved.")
            rows.extend(batch)
            if len(batch) < settings.universe.screener_page_size:
                break
        else:
            raise BacktestError("Universe pagination incomplete; no snapshot saved.")
    # Only fields observed in this response become dated metadata.
    stocks = [{"symbol": s.symbol, "companyName": s.name, "exchange": s.exchange, "industry": s.industry,
               "sector": s.sector, "isEtf": s.is_etf, "isFund": s.is_fund, "avgVolume": s.avg_volume}
              for s in parse_listings(rows)]
    observed = fixed_now or datetime.now(timezone.utc)
    return import_archive(store, {"schema_version": 1, "universes": [
        {"date": day, "observed_at": observed.isoformat(), "source": "FMP company-screener, fresh complete exchange requests",
         "complete": True, "stocks": stocks}]}, now=observed)


def _universe_by_day(store, days: list[str]) -> dict[str, dict]:
    wanted = set(days)
    snapshots = {}
    for observation in store.vintages("universe"):
        day = observation["item"]
        if day in wanted and timestamp(observation["observed_at"]) <= session_close(day):
            snapshots[day] = observation
    return snapshots


def coverage(store, settings: Settings, start: str, end: str) -> dict:
    """Cheap preflight; daily rank and survivor checks run in the engine."""
    if date.fromisoformat(start) >= date.fromisoformat(end):
        raise BacktestError("Start date must precede end date.")
    if end > eod_cutoff(datetime.now(timezone.utc), settings.prices.eod_final_hour_et).isoformat():
        raise BacktestError("End date must be a completed daily session or earlier.")
    spy = store.price_history("SPY", through=end)
    days = [b.date for b in spy if start <= b.date <= end]
    errors = []
    if len(days) < 2:
        errors.append("Need at least two cached SPY sessions within this period.")
    if days and (date.fromisoformat(days[0]) - date.fromisoformat(start)).days > 3:
        errors.append("SPY history starts after the requested period. Run the price backfill.")
    if not spy or spy[-1].date < end and (date.fromisoformat(end) - date.fromisoformat(spy[-1].date)).days > 3:
        errors.append("SPY history does not reach the requested end. Run the price backfill.")
    snapshots = _universe_by_day(store, days)
    # Last session has no opening order to schedule, but still needs a dated
    # universe for coverage, delistings and a reproducible full-period archive.
    missing = [d for d in days if d not in snapshots]
    if missing:
        errors.append(f"Missing dated universe snapshots for {len(missing)} sessions, starting {missing[0]}. "
                      "Import historical observations; current company metadata cannot fill these dates.")
    delist = store.document("backtest:delisted")
    if not delist or delist.get("complete") is not True or delist.get("fetched_at", "")[:10] < end:
        errors.append("Refresh the complete delisted-company directory through the requested end.")
    symbols = sorted({s["symbol"] for v in snapshots.values() for s in v["body"]["stocks"]})
    return {"ready": not errors, "errors": errors, "sessions": len(days), "first_session": days[0] if days else None,
            "last_session": days[-1] if days else None, "universe_sessions": len(snapshots),
            "archive_symbols": len(symbols), "delisted_records": len(delist.get("rows", [])) if delist else 0}


def run_backtest(store, settings: Settings, start: str, end: str, *, portfolio: BacktestSettings | None = None,
                 sample: int | None = None, progress=None, label: str | None = None) -> dict:
    status = coverage(store, settings, start, end)
    if not status["ready"]:
        raise BacktestError("\n".join(status["errors"]))
    if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int) or sample < 1):
        raise BacktestError("Development sample must be a positive integer.")
    t, portfolio = settings.thresholds, portfolio or settings.backtest
    market_histories = {s: store.price_history(s, through=end) for s in ("SPY", "QQQ", "^GSPC", "^IXIC")}
    all_sessions = [b.date for b in market_histories["SPY"]]
    days = [d for d in all_sessions if start <= d <= end]
    other_sessions = {b.date for symbol, bars in market_histories.items() if symbol != "SPY"
                      for b in bars if start <= b.date <= end}
    if not other_sessions <= set(days):
        raise BacktestError("SPY session calendar has gaps relative to the index histories. Refresh market prices.")
    min_warmup = max(t.high_sessions, t.long_ma_sessions, t.short_ma_sessions, t.volume_sessions,
                     t.rs_quarter_sessions * 4 + 1, t.industry_sessions + 1, t.distribution_window_sessions + 1)
    if bisect_right(all_sessions, days[0]) < min_warmup:
        raise BacktestError(f"Need at least {min_warmup} SPY sessions through the first test date for warm-up.")
    market = market_direction(market_histories, t, as_of=end)
    states = {row["date"]: row["state"] for row in market["history"]}
    exposures = {row["date"]: row["exposure"] for row in market["history"]}
    if any(states.get(day) is None for day in days):
        raise BacktestError("Market direction has missing index/volume evidence in the selected period.")
    snapshots = _universe_by_day(store, days)
    delist_rows = store.document("backtest:delisted")["rows"]
    delisted, listing_rows = {}, []
    for row in delist_rows:
        if row.get("exchange") not in settings.universe.exchanges:
            continue
        try:
            first, last = date.fromisoformat(row["ipoDate"]), date.fromisoformat(row["delistedDate"])
            symbol = row["symbol"]
            if first > last:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise BacktestError("A US delisting record lacks valid listing dates. Resolve it before backtesting.") from None
        if last.isoformat() <= days[0] or first.isoformat() > days[-1]:
            continue
        if symbol in delisted and delisted[symbol] != last.isoformat():
            raise BacktestError(f"{symbol}: multiple listing lifetimes need a stable security identity.")
        delisted[symbol] = last.isoformat()
        listing_rows.append(row)
    universe = {}
    for day, snapshot in snapshots.items():
        stocks = parse_listings(snapshot["body"]["stocks"])
        observed_symbols = {s.symbol for s in stocks}
        missing = [r["symbol"] for r in listing_rows if r["ipoDate"] <= day < r["delistedDate"]
                   and r["symbol"] not in observed_symbols and exclusion_reason(
                       r["symbol"], r.get("companyName"), exchange=r["exchange"], is_etf=False, is_fund=False) is None]
        if missing:
            raise BacktestError(f"{day}: universe omits formerly listed companies: {', '.join(missing[:8])}.")
        universe[day] = {s.symbol: s for s in stocks if s.is_common and s.exchange in settings.universe.exchanges
                         and day < delisted.get(s.symbol, "9999-12-31")}
        if not universe[day]:
            raise BacktestError(f"{day}: empty historical common-stock universe.")
    symbols = sorted({s for members in universe.values() for s in members})
    histories = {s: store.price_history(s, through=end) for s in symbols}
    state = store.price_states(symbols)
    splits, basis_dates = {}, {}
    vintages = defaultdict(list)
    for vintage in store.vintages("fundamentals"):
        if vintage["item"] in histories:
            vintages[vintage["item"]].append(vintage)
    for symbol in symbols:
        reference = store.document(f"backtest:splits:{symbol}")
        if not histories[symbol] or symbol not in state or not state[symbol].checked_at:
            raise BacktestError(f"{symbol}: no price history or adjustment-basis date. Backfill this historical member.")
        basis_dates[symbol] = state[symbol].checked_at.astimezone(ET).date().isoformat()
        needed = all_sessions[max(0, bisect_right(all_sessions, days[0]) - min_warmup)]
        if not state[symbol].requested_from or state[symbol].requested_from > needed:
            raise BacktestError(f"{symbol}: price backfill does not cover the ranking warm-up beginning {needed}.")
        if reference is None or reference["fetched_on"] < basis_dates[symbol]:
            raise BacktestError(f"{symbol}: split history must cover its price adjustment basis. Prepare backtest data.")
        splits[symbol] = reference["rows"]
    corrections = frozenset(row["date"] for row in market["history"] if row["state"] == CORRECTION)
    counts = Counter()
    diagnostics = []

    def signals_on(day):
        sessions = all_sessions[:bisect_right(all_sessions, day)]
        metrics, adjusted, factors = {}, {}, {}
        for symbol in universe[day]:
            cutoff = (date.fromisoformat(day) - timedelta(weeks=history_weeks(t) + 1)).isoformat()
            cutoff = min(cutoff, sessions[max(0, len(sessions) - min_warmup)])
            recent = [b for b in histories[symbol] if cutoff <= b.date <= day]
            adjusted[symbol], factors[symbol] = restore_price_basis(recent, splits[symbol], basis_dates[symbol], day)
            if not adjusted[symbol] or adjusted[symbol][-1].date != day:
                raise BacktestError(f"{symbol}: missing historical member price on {day}; cannot rank a reduced market.")
            observed_days = {b.date for b in adjusted[symbol]}
            expected_days = {d for d in sessions[-min_warmup:] if d >= histories[symbol][0].date}
            if not expected_days <= observed_days:
                raise BacktestError(f"{symbol}: price gaps in the ranking window ending {day}.")
            metrics[symbol] = price_metrics(adjusted[symbol], sessions, t)
        ratings = rs_ratings({s: m.rs_score for s, m in metrics.items()})
        groups = industry_ranks({s: row.industry for s, row in universe[day].items()},
                                {s: m.industry_return for s, m in metrics.items()})
        counts["unrated_member_sessions"] += len(metrics) - len(ratings)
        if any(not universe[day][s].industry for s, m in metrics.items() if m.industry_return is not None):
            raise BacktestError(f"{day}: historical industry classification is missing for an eligible member.")
        survivors = [s for s, m in metrics.items() if m.price_pass and ratings.get(s, 0) >= t.rs_min]
        counts["price_rs_survivor_sessions"] += len(survivors)
        if sample:
            survivors = sample_symbols(survivors, sample, settings.prices.sample_seed)
        signals = []
        for symbol in survivors:
            bundle = available_bundle(vintages[symbol], day)
            if bundle is None:
                raise BacktestError(f"{symbol} on {day}: no archived statement vintage known by that session. "
                                    "Today's restated statements cannot substitute for historical observations.")
            criteria, warnings = score_fundamentals(bundle, t, date.fromisoformat(day))
            criteria += leadership_criteria(ratings[symbol], groups.get(universe[day][symbol].industry), t)
            if any(c.passed is None for c in criteria):
                counts["unavailable_survivor_sessions"] += 1
            if not meets_rule(criteria, t):
                continue
            base = detect_base(adjusted[symbol], day, t, correction_dates=frozenset(d for d in corrections if d <= day),
                               sessions=sessions)
            technical = technical_criteria(base, {"state": states[day], "exposure": exposures[day], "as_of": day}, t, day)
            if buyable(base, day, t) and all(c.passed is True for c in technical):
                evidence = {"criteria": [asdict(c) for c in criteria + technical], "base": base, "warnings": warnings,
                            "universe_observed_at": snapshots[day]["observed_at"], "historical_price_factor": factors[symbol]}
                signals.append(Signal(symbol, day, base["pivot"] / factors[symbol], ratings[symbol], base["pattern"], evidence,
                                      base["breakout_date"]))
        counts["signals"] += len(signals)
        diagnostics.append({"date": day, "members": len(metrics), "ranked": len(ratings),
                            "survivors": len(survivors), "signals": len(signals)})
        return signals

    settlements = {}
    for row in (store.document("backtest:settlements") or {}).get("rows", []):
        if row["symbol"] in basis_dates:
            if row["basis_date"] != basis_dates[row["symbol"]]:
                raise BacktestError(f"{row['symbol']}: settlement basis differs from cached price basis.")
            settlements[row["symbol"], row["date"]] = row["cash_per_share"]
    report = simulate(days, {**histories, "SPY": market_histories["SPY"]}, states, signals_on, t, portfolio,
                      exposure=exposures,
                      settlements=settlements, delisted=delisted, progress=progress)
    report.update(mode="strict", label=label or None, created_at=datetime.now(timezone.utc).isoformat(), api_calls=0, sample=sample,
                  limitations=LIMITATIONS, coverage=status, signal_counts=dict(counts), daily_screen=diagnostics,
                  market_proxy_sessions={s: sum(d["volume_source"] == info["proxy"] for d in info["history"]
                                                if days[0] <= d["date"] <= days[-1]) for s, info in market["indexes"].items()})
    report["input_digest"] = hashlib.sha256(json.dumps({"snapshots": snapshots, "vintages": vintages,
        "prices": {s: [asdict(b) for b in bars] for s, bars in {**histories, **market_histories}.items()},
        "splits": splits, "basis_dates": basis_dates, "delisted": delist_rows, "settlements": list(settlements.items())},
        sort_keys=True).encode()).hexdigest()
    store.save_document("latest_backtest", report)
    store.save_document(f"backtest:result:{report['created_at']}", report)
    return report


def prepare_splits(client, store, settings: Settings, start: str, end: str, *, sample: int | None = None, progress=None) -> dict:
    """Prepare corporate actions for archived members, including delisted ones."""
    observations = [v for v in store.vintages("universe") if start <= v["item"] <= end]
    if not observations:
        raise BacktestError("Import dated universe snapshots before preparing historical members.")
    symbols = sorted({s.symbol for v in observations for s in parse_listings(v["body"]["stocks"])
                      if s.is_common and s.exchange in settings.universe.exchanges})
    if sample is not None:
        if sample < 1:
            raise BacktestError("Sample must be positive.")
        symbols = sample_symbols(symbols, sample, settings.prices.sample_seed)
    initial = client.stats.api_calls
    today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    for i, symbol in enumerate(symbols):
        cached = store.document(f"backtest:splits:{symbol}")
        if not cached or cached["fetched_on"] < today:
            rows = client.splits(symbol)
            if not isinstance(rows, list) or split_factor(rows, date.min, date.max) is None:
                raise BacktestError(f"{symbol}: invalid split response.")
            store.save_document(f"backtest:splits:{symbol}", {"fetched_on": today, "rows": rows})
        if progress:
            progress(f"Corporate actions {i + 1}/{len(symbols)}", client.stats.api_calls - initial)
    return {"symbols": len(symbols), "api_calls": client.stats.api_calls - initial}
