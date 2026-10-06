"""Universe refresh and price-history backfill.

The first run downloads ``prices.history_years`` of daily bars for every
symbol. Later runs fetch only the days since each symbol's last stored bar,
so the same command serves as the daily update.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .config import PriceSettings, Settings, UniverseSettings
from .fmp import FMPAuthError, FMPClient, FMPError, FMPPlanRestrictedError
from .prices import (
    Bar,
    PriceJob,
    eod_cutoff,
    history_start,
    is_restated,
    last_eod_release,
    parse_bars,
    plan_price_job,
)
from .store import Store
from .universe import parse_listings, sample_symbols

log = logging.getLogger(__name__)

# FMP returns at most 5,000 rows (about 19.8 years of sessions) per call.
MAX_DAYS_PER_CALL = 7000
MAX_SCREENER_PAGES = 50

ProgressCallback = Callable[[int, int, dict[str, int]], None]


class BackfillError(RuntimeError):
    pass


# ----------------------------------------------------------------------
# Universe


@dataclass
class UniverseReport:
    listed: int
    common: int
    by_exchange: dict[str, int]
    excluded: dict[str, int]


def refresh_universe(client: FMPClient, store: Store, settings: UniverseSettings) -> UniverseReport:
    ttl = timedelta(hours=settings.refresh_hours)
    rows: list[dict] = []
    for exchange in settings.exchanges:
        for page in range(MAX_SCREENER_PAGES):
            batch = client.company_screener(
                exchange=exchange,
                include_all_share_classes=settings.include_all_share_classes,
                page=page,
                limit=settings.screener_page_size,
                cache_ttl=ttl,
            )
            rows.extend(batch)
            if len(batch) < settings.screener_page_size:
                break
        else:
            log.warning("Stopped paging %s after %d screener pages", exchange, MAX_SCREENER_PAGES)
    stocks = parse_listings(rows)
    if not stocks:
        raise BackfillError("FMP's screener returned no symbols; the stored universe was kept.")
    store.replace_universe(stocks)
    common = [s for s in stocks if s.is_common]
    return UniverseReport(
        listed=len(stocks),
        common=len(common),
        by_exchange=dict(sorted(Counter(s.exchange or "?" for s in common).items())),
        excluded=dict(Counter(s.excluded_reason for s in stocks if not s.is_common).most_common()),
    )


# ----------------------------------------------------------------------
# Prices


@dataclass(frozen=True)
class PriceFetchResult:
    symbol: str
    bars: list[Bar]
    replace: bool
    requested_from: date | None
    status: str
    restated: bool = False
    error: str | None = None


@dataclass
class PriceSyncReport:
    symbols: int = 0
    fetched: int = 0
    already_current: int = 0
    bars_written: int = 0
    restated: int = 0
    empty: int = 0
    restricted: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def fetch_bars(client: FMPClient, symbol: str, start: date, end: date) -> list[Bar]:
    """Daily bars for [start, end], split into calls FMP can serve whole."""
    bars: dict[str, Bar] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=MAX_DAYS_PER_CALL - 1))
        for bar in parse_bars(client.historical_prices(symbol, chunk_start, chunk_end)):
            bars[bar.date] = bar
        chunk_start = chunk_end + timedelta(days=1)
    return [bars[d] for d in sorted(bars)]


def run_price_job(
    client: FMPClient, job: PriceJob, cutoff: date, tolerance_pct: float
) -> PriceFetchResult:
    last_final = cutoff.isoformat()

    def final_bars(start: date) -> list[Bar]:
        return [b for b in fetch_bars(client, job.symbol, start, cutoff) if b.date <= last_final]

    try:
        if job.full:
            bars = final_bars(job.start)
            return PriceFetchResult(
                job.symbol, bars, True, job.requested_from, "ok" if bars else "empty"
            )
        bars = final_bars(job.start)
        overlap_date, overlap_close = job.overlap
        match = next((b for b in bars if b.date == overlap_date), None)
        if bars and (match is None or is_restated(overlap_close, match.close, tolerance_pct)):
            log.info(
                "%s: history re-adjusted since last fetch (close on %s was %s, now %s);"
                " downloading full history",
                job.symbol, overlap_date, overlap_close, match.close if match else "missing",
            )
            bars = final_bars(job.requested_from)
            return PriceFetchResult(job.symbol, bars, True, job.requested_from, "ok", restated=True)
        new_bars = [b for b in bars if b.date > overlap_date]
        return PriceFetchResult(job.symbol, new_bars, False, None, "ok")
    except FMPAuthError:
        raise
    except FMPPlanRestrictedError as exc:
        return PriceFetchResult(job.symbol, [], False, None, "restricted", error=str(exc))
    except FMPError as exc:
        return PriceFetchResult(job.symbol, [], False, None, "error", error=str(exc))


def sync_prices(
    client: FMPClient,
    store: Store,
    symbols: Sequence[str],
    settings: PriceSettings,
    *,
    now: datetime,
    workers: int,
    progress: ProgressCallback | None = None,
) -> PriceSyncReport:
    cutoff = eod_cutoff(now, settings.eod_final_hour_et)
    release = last_eod_release(now, settings.eod_final_hour_et)
    desired_start = history_start(cutoff, settings.history_years)
    states = store.price_states(symbols)

    report = PriceSyncReport(symbols=len(symbols))
    jobs = []
    for symbol in symbols:
        job = plan_price_job(
            symbol, states.get(symbol), desired_start=desired_start, cutoff=cutoff, release=release
        )
        if job is None:
            report.already_current += 1
        else:
            jobs.append(job)
    log.info(
        "Prices: %d symbols, %d already current, %d to fetch (bars through %s)",
        len(symbols), report.already_current, len(jobs), cutoff,
    )
    if not jobs:
        return report

    step = max(1, len(jobs) // 20)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [
            pool.submit(run_price_job, client, job, cutoff, settings.restate_tolerance_pct)
            for job in jobs
        ]
        for done, future in enumerate(as_completed(futures), 1):
            result = future.result()
            report.bars_written += store.write_prices(
                result.symbol,
                result.bars,
                replace=result.replace,
                requested_from=result.requested_from.isoformat() if result.requested_from else None,
                checked_at=now,
                status=result.status,
                error=result.error,
            )
            report.fetched += 1
            report.restated += result.restated
            if result.status == "empty":
                report.empty += 1
            elif result.status == "restricted":
                report.restricted.append(result.symbol)
            elif result.status == "error":
                report.errors[result.symbol] = result.error or "unknown error"
                log.warning("%s: %s", result.symbol, result.error)
            stats = client.stats.snapshot()
            if progress:
                progress(done, len(jobs), stats)
            if done % step == 0 or done == len(jobs):
                log.info(
                    "Prices: %d/%d symbols fetched, %d API calls so far",
                    done, len(jobs), stats["api_calls"],
                )
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    report.restricted.sort()
    return report


# ----------------------------------------------------------------------
# Backfill command


@dataclass
class BackfillReport:
    started_at: str
    elapsed_seconds: float
    universe: UniverseReport | None
    prices: PriceSyncReport
    api_calls: int
    cache_hits: int
    retries: int

    def as_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        minutes, seconds = divmod(round(self.elapsed_seconds), 60)
        p = self.prices
        lines = [f"Backfill finished in {minutes}m {seconds:02d}s"]
        if self.universe:
            u = self.universe
            exchanges = ", ".join(f"{k} {v:,}" for k, v in u.by_exchange.items())
            excluded = ", ".join(f"{k} {v:,}" for k, v in u.excluded.items()) or "none"
            lines.append(f"  Universe: {u.common:,} common stocks ({exchanges}) out of {u.listed:,} listed")
            lines.append(f"  Excluded: {excluded}")
        lines.append(
            f"  Prices: {p.symbols:,} symbols, {p.fetched:,} fetched, {p.already_current:,} already"
            f" current, {p.bars_written:,} bars written, {p.restated:,} re-downloaded after"
            f" re-adjustment, {p.empty:,} with no data"
        )
        if p.restricted:
            shown = ", ".join(p.restricted[:10]) + (" ..." if len(p.restricted) > 10 else "")
            lines.append(f"  Not available on this FMP plan: {len(p.restricted):,} ({shown})")
        if p.errors:
            lines.append(f"  Errors: {len(p.errors):,} (see log)")
        lines.append(
            f"  API calls this session: {self.api_calls:,}"
            f" (cache hits {self.cache_hits:,}, retries {self.retries:,})"
        )
        return "\n".join(lines)


def run_backfill(
    client: FMPClient,
    store: Store,
    settings: Settings,
    *,
    symbols: Sequence[str] | None = None,
    sample: int | None = None,
    refresh: bool = True,
    workers: int | None = None,
    now: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> BackfillReport:
    """Refresh the universe (unless ``symbols`` is given) and bring price
    history up to date for the universe plus the market index symbols."""
    now = now or datetime.now().astimezone()
    started = time.monotonic()
    run_id = store.start_run(
        "backfill", {"symbols": list(symbols) if symbols else None, "sample": sample}
    )
    calls_before = client.stats.snapshot()

    try:
        universe_report = None
        if symbols:
            targets = sorted({s.strip().upper() for s in symbols if s.strip()})
        else:
            if refresh:
                universe_report = refresh_universe(client, store, settings.universe)
                log.info(
                    "Universe: %d common stocks of %d listed",
                    universe_report.common, universe_report.listed,
                )
            targets = store.universe_symbols()
            if not targets:
                raise BackfillError(
                    "The universe is empty. Run a backfill without --skip-universe first."
                )
            # Open positions keep daily prices even outside the common-stock universe.
            targets = sorted({*targets, *(p["symbol"] for p in store.positions())})
        if sample:
            targets = sample_symbols(targets, sample, settings.prices.sample_seed)
        targets = list(dict.fromkeys([*settings.prices.market_symbols, *targets]))

        prices_report = sync_prices(
            client, store, targets, settings.prices,
            now=now, workers=workers or settings.fmp.max_workers, progress=progress,
        )
    except BaseException as exc:
        calls = client.stats.snapshot()["api_calls"] - calls_before["api_calls"]
        store.finish_run(run_id, {"failed": f"{type(exc).__name__}: {exc}", "api_calls": calls})
        raise
    calls_after = client.stats.snapshot()
    report = BackfillReport(
        started_at=now.isoformat(timespec="seconds"),
        elapsed_seconds=time.monotonic() - started,
        universe=universe_report,
        prices=prices_report,
        api_calls=calls_after["api_calls"] - calls_before["api_calls"],
        cache_hits=calls_after["cache_hits"] - calls_before["cache_hits"],
        retries=calls_after["retries"] - calls_before["retries"],
    )
    store.finish_run(run_id, report.as_dict())
    return report
