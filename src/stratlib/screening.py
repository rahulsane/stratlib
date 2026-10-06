"""Staged screening orchestration. Arithmetic lives in pure modules."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Settings
from .fmp import FMPAuthError, FMPClient, FMPError, FMPPlanRestrictedError
from .fundamentals import load_fundamentals
from .market_stats import industry_ranks, price_metrics, rs_ratings
from .prices import ET, eod_cutoff
from .scoring import leadership_criteria, meets_rule, score_fundamentals, unscored_fundamentals
from .store import Store
from .technical import load_market, stock_bases, technical_criteria
from .universe import sample_symbols

log = logging.getLogger(__name__)


class ScreenError(RuntimeError):
    pass


def run_screen(store: Store, settings: Settings, client: FMPClient | None = None,
               *, sample: int | None = None, now: datetime | None = None,
               progress: Callable[[str, int, int, int], None] | None = None,
               strategy_id: str = "canslim", workers: int = 1) -> dict:
    """Rank the full stored universe, then limit expensive survivor work.

    ``client=None`` is explicitly cache-only. No price or universe downloads
    occur here; ``backfill`` owns the daily update. A sample never changes RS
    or industry ranks. It selects from survivors after the full ranking.
    ``workers`` processes share the weekly base search; see
    ``technical.stock_bases`` before raising it from a script.
    """
    from .strategies import project_screen, strategy
    spec = strategy(strategy_id)
    if spec.scan:
        # Scan strategies apply their own rules to the cached bars; the sample option belongs to C/A/S/L.
        from .scanning import run_scan
        return run_scan(store, settings, client, strategy_id=strategy_id, now=now, progress=progress)
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    cutoff = eod_cutoff(now, settings.prices.eod_final_hour_et)
    t = settings.thresholds
    if sample is not None and sample < 1:
        raise ScreenError("Sample must be a positive number.")
    universe = store.universe()
    if not universe:
        raise ScreenError("No stored universe. Run stratlib backfill first.")
    needed = max(t.high_sessions, t.long_ma_sessions, t.short_ma_sessions,
                 t.volume_sessions, t.rs_quarter_sessions * 4 + 1, t.industry_sessions + 1)
    reference = store.price_history("SPY", limit=needed, through=cutoff.isoformat())
    if len(reference) < needed:
        raise ScreenError(f"SPY needs {needed} completed sessions for the ranking calendar. Run stratlib backfill.")
    sessions = [b.date for b in reference]
    as_of = sessions[-1]
    market = load_market(store, t, as_of)
    initial_calls = client.stats.api_calls if client else 0

    def update(message, done, total):
        if progress:
            progress(message, done, total, client.stats.api_calls - initial_calls if client else 0)

    metrics = {}
    missing = []
    stale = []
    for i, stock in enumerate(universe):
        # Provider-only dates must not displace an actual SPY session/RS anchor.
        bars = store.price_history(stock["symbol"], since=sessions[0], through=as_of)
        if not bars:
            # Distinguish a stale history outside this window from a missing backfill.
            bars = store.price_history(stock["symbol"], limit=1, through=as_of)
        if not bars:
            missing.append(stock["symbol"])
        elif bars[-1].date != as_of:
            stale.append(stock["symbol"])
        metrics[stock["symbol"]] = price_metrics(bars, sessions, t)
        if i % 100 == 0 or i == len(universe) - 1:
            update("Ranking the full universe", i + 1, len(universe))
    if missing:
        raise ScreenError(f"{len(missing)} universe stocks have no price history. Complete the full backfill before market-wide RS. Examples: {', '.join(missing[:5])}")
    ranks = rs_ratings({s: m.rs_score for s, m in metrics.items()})
    groups = industry_ranks({s["symbol"]: s["industry"] for s in universe},
                            {s: m.industry_return for s, m in metrics.items()})
    survivors = [s["symbol"] for s in universe if metrics[s["symbol"]].price_pass
                 and ranks.get(s["symbol"], 0) >= t.rs_min]
    selected = set(sample_symbols(survivors, sample, settings.prices.sample_seed) if sample else survivors)
    warnings = list(market["warnings"])
    if (cutoff - datetime.fromisoformat(as_of).date()).days > 4:
        warnings.append(f"Stored prices are stale. Results use {as_of}; run the daily backfill.")
    if stale:
        warnings.append(f"{len(stale)} stocks have no bar on {as_of} and are excluded from ranks: {', '.join(stale[:10])}.")
    if len(ranks) < len(universe):
        warnings.append(f"RS covers {len(ranks):,} of {len(universe):,} stocks. Short or incomplete histories receive no rating.")
    if sample:
        warnings.append(f"Development sample: fundamentals evaluated for {len(selected)} of {len(survivors)} survivors. Ranks still use the full universe.")
    if client is None:
        warnings.append("Cache-only run. Statements and the earnings calendar were not refreshed.")
    earnings = []
    if client and selected:
        monday = today - timedelta(days=today.weekday())
        update("Checking this week's earnings calendar", 0, len(selected))
        earnings = client.earnings_calendar(monday, monday + timedelta(days=6))
        if not isinstance(earnings, list):
            raise ScreenError("Unexpected earnings calendar response; no result was saved.")
    # The weekly base search is most of a screen's work and each stock's is independent, so it runs first,
    # spread over the worker processes.
    bases = stock_bases(store, [s["symbol"] for s in universe], as_of, t, market, workers=workers,
                        progress=lambda done, total: update("Detecting weekly bases", done, total))
    run_id = store.start_run("screen", {"sample": sample, "cache_only": client is None})
    rows = []
    done = 0
    previous_callback = client.on_call if client else None
    if client:
        def on_call(stats):
            if previous_callback:
                previous_callback(stats)
            update("Fetching survivor fundamentals", done, len(selected))
        client.on_call = on_call
    try:
        for stock_index, stock in enumerate(universe):
            symbol = stock["symbol"]
            m = metrics[symbol]
            error = None
            notes = []
            fetched_on = None
            if symbol in selected:
                try:
                    bundle = load_fundamentals(client, store, symbol, earnings, today)
                    if bundle:
                        criteria, notes = score_fundamentals(bundle, t, today)
                        fetched_on = bundle["fetched_on"]
                        stage = "Scored"
                    else:
                        criteria = unscored_fundamentals("No cached statements. Run with the statement refresh enabled.")
                        stage = "Missing fundamentals"
                except (FMPAuthError, FMPPlanRestrictedError):
                    # Plan/auth failures are global and must not become thousands
                    # of calls, or silently substituted data sources.
                    raise
                except FMPError as exc:
                    error = str(exc)
                    criteria = unscored_fundamentals(error)
                    stage = "Data error"
                done += 1
                update(f"Evaluated {symbol}", done, len(selected))
            else:
                stage = "Outside sample" if symbol in survivors else "Price filter" if not m.price_pass else "RS filter"
                criteria = unscored_fundamentals(f"Not fetched: {stage}.")
            criteria += leadership_criteria(ranks.get(symbol), groups.get(stock["industry"]), t)
            passed = m.price_pass and meets_rule(criteria, t)
            base = bases[symbol]
            technical = technical_criteria(base, market, t, as_of)
            criteria += technical
            phase3_pass = passed and all(c.passed is True for c in technical)
            rows.append({**stock, "prices": asdict(m), "rs": ranks.get(symbol),
                         "industry_stats": groups.get(stock["industry"]), "stage": stage,
                         "phase2_pass": passed, "phase3_pass": phase3_pass, "base": base,
                         "criteria": [c.to_dict() for c in criteria],
                         "warnings": notes, "error": error, "fundamentals_fetched_on": fetched_on})
            if stock_index % 100 == 0 or stock_index == len(universe) - 1:
                update("Evaluating the N and M checks", stock_index + 1, len(universe))
        rows.sort(key=lambda r: (r["stage"] != "Scored", not r["phase2_pass"], -(r["rs"] or 0), r["symbol"]))
        report = {
            "run_id": run_id, "created_at": now.isoformat(), "price_date": as_of,
            "fundamental_as_of": today.isoformat(), "universe_count": len(universe),
            "ranked_count": len(ranks), "industry_count": len(groups),
            "price_pass_count": sum(m.price_pass for m in metrics.values()),
            "survivor_count": len(survivors), "evaluated_count": len(selected),
            "pass_count": sum(r["phase2_pass"] for r in rows), "sample": sample,
            "phase": 3, "phase3_pass_count": sum(r["phase3_pass"] for r in rows),
            "base_count": sum(r["base"]["pattern"] is not None for r in rows), "market": market,
            "cache_only": client is None, "warnings": warnings,
            "api_calls": client.stats.api_calls - initial_calls if client else 0,
            "elapsed_seconds": time.monotonic() - started, "thresholds": asdict(t), "rows": rows,
        }
        report = project_screen(report, strategy_id, t, settings.backtest)
        report = store.save_screen(report)
        store.finish_run(run_id, {k: v for k, v in report.items() if k not in {"rows", "market"}})
        log.info("Screen finished: %d survivors, %d evaluated, %d API calls, %.1fs",
                 len(survivors), len(selected), report["api_calls"], report["elapsed_seconds"])
        return report
    except BaseException as exc:
        store.finish_run(run_id, {"status": "failed", "error": type(exc).__name__})
        raise
    finally:
        if client:
            client.on_call = previous_callback
