"""Command line entry point: ``stratlib backfill``, ``stratlib status``, ``stratlib web``, ``stratlib publish`` and
``stratlib tradetest-bank``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .app import open_context
from .backfill import BackfillError, run_backfill
from .backtest import BacktestError
from .config import ConfigError, Settings, load_fmp_api_key, load_settings
from .fmp import FMPAuthError, FMPError, FMPPlanRestrictedError, SharedRateLimiter
from .logging_setup import configure_logging
from .parallel import all_cores
from .store import Store
from .strategies import STRATEGIES

log = logging.getLogger("stratlib")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="stratlib", description="StratLib screener data tools")
    parser.add_argument("--config", help="path to config.yaml (default: project root)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every API call")
    sub = parser.add_subparsers(dest="command", required=True)

    bf = sub.add_parser(
        "backfill",
        help="refresh the universe and download or extend daily price history",
        description=(
            "First run: downloads the universe and prices.history_years of daily bars. "
            "Later runs fetch only what is new, so this is also the daily update."
        ),
    )
    bf.add_argument("--sample", type=int, metavar="N",
                    help="limit the run to a repeatable sample of N universe symbols")
    bf.add_argument("--symbols", metavar="A,B,C",
                    help="run on these tickers only; skips the universe refresh")
    bf.add_argument("--skip-universe", action="store_true",
                    help="reuse the stored universe instead of refreshing it")
    bf.add_argument("--workers", type=int, help="concurrent requests (default: fmp.max_workers)")

    sub.add_parser("status", help="show what is in the local database and today's API usage")
    screen = sub.add_parser("screen", help="rank the whole cached market and screen survivors")
    screen.add_argument("--sample", type=int, metavar="N", help="limit fundamentals to N survivors; ranks remain market-wide")
    screen.add_argument("--offline", action="store_true", help="use cached statements only; no API key required")
    screen.add_argument("--strategy", default="canslim", choices=list(STRATEGIES),
                        help="strategy whose rules the screen applies (default: canslim)")
    screen.add_argument("--workers", type=int, help="processes for the weekly base search (default: all cores)")
    for name in ("web", "gui"):
        web = sub.add_parser(name, help="start the web app at http://127.0.0.1:8600"
                             + (" (same as web)" if name == "gui" else ""))
        web.add_argument("--port", type=int, default=8600, help="local port (default 8600)")
        web.add_argument("--host", default="127.0.0.1",
                         help="address to listen on (default 127.0.0.1, this computer only; 0.0.0.0 for every interface)")
        web.add_argument("--open", action="store_true", help="open it in the browser")
        web.add_argument("--public", action="store_true",
                         help="the read-only public app: no runs, edits, positions, settings or API calls")
        web.add_argument("--db", metavar="PATH", help="read this database instead of data.db_path, such as a snapshot")
    pub = sub.add_parser("publish", help="write a read-only snapshot of the database for the public web app")
    pub.add_argument("--output", metavar="PATH", help="snapshot file (default: public/stratlib.db beside config.yaml)")
    pub.add_argument("--years", type=float, default=4.0,
                     help="years of daily prices kept per stock (default 4); market symbols keep their whole history")
    bank = sub.add_parser("tradetest-bank", help="build the TradeTest chart bank: random daily windows for the blind replay")
    bank.add_argument("--windows", type=int, default=4000, help="windows in the bank (default 4000)")
    bank.add_argument("--seed", type=int, default=20261008, help="random seed, so a build can be repeated (default 20261008)")
    bank.add_argument("--output", metavar="PATH", help="bank file (default: public/tradetest_bank.npz beside config.yaml)")
    bank.add_argument("--etf-cache", metavar="PATH",
                      help="daily ETF bars as JSON, used only for allowlisted ETFs the database lacks (default: "
                           "research/cache/supply_demand_all_daily.json beside config.yaml)")
    bank.add_argument("--db", metavar="PATH", help="read this database instead of data.db_path")
    bt = sub.add_parser("backtest", help="replay historical CANSLIM rules from dated cached inputs")
    bt.add_argument("--start", required=True, help="first date, YYYY-MM-DD")
    bt.add_argument("--end", required=True, help="last date, YYYY-MM-DD")
    bt.add_argument("--sample", type=int, help="development cap on survivors per day; ranks still use all historical members")
    bt.add_argument("--max-holdings", type=int)
    bt.add_argument("--capital", type=float)
    bt.add_argument("--check", action="store_true", help="check archive coverage without running")
    bt.add_argument("--approximate", action="store_true",
                    help="use today's statement histories dated by filing, plus delisted companies (see README)")
    bt.add_argument("--strategy", choices=("breakout", "leaders", "trend"), default="breakout",
                    help="approximate method only: breakout trades, the leaders portfolio or trend leaders")
    bt.add_argument("--label", help="name for the saved result, shown on the Backtest page")
    bt.add_argument("--workers", type=int, help="processes for the approximate method (default: all cores)")
    bt.add_argument("--output", help="also write the full saved result as JSON")
    archive = sub.add_parser("backtest-import", help="import dated universe and statement observations")
    archive.add_argument("file")
    prep = sub.add_parser("backtest-prepare", help="cache delistings, prices and splits for archived members")
    prep.add_argument("--start", required=True)
    prep.add_argument("--end", required=True)
    prep.add_argument("--sample", type=int)
    sub.add_parser("backtest-capture", help="capture today's FMP universe, before 13:00 Eastern")
    sub.add_parser("backtest-delistings", help="cache the complete FMP delisting directory")
    approx = sub.add_parser("backtest-approx-prepare",
                            help="fetch delisted companies, splits and statement histories for an approximate backtest")
    approx.add_argument("--start", required=True)
    approx.add_argument("--end", required=True)
    study = sub.add_parser("backtest-rule-study",
                           help="measure which screening rules predicted later returns (approximate data, no API calls)")
    study.add_argument("--start", required=True)
    study.add_argument("--end", required=True)
    snap = sub.add_parser("estimates-snapshot",
                          help="save this month's consensus estimates and MSCI GARP holdings files (about 610 API calls)")
    snap.add_argument("--force", action="store_true", help="take it again even if this month's is already saved")
    engine = sub.add_parser("strategy-backtest",
                            help="backtest the workspace strategies on one engine under the research ground rules, 2016 on")
    engine.add_argument("--strategy", action="append", choices=list(STRATEGIES),
                        help="a strategy to run (repeatable); default: all of them")
    engine.add_argument("--workers", type=int, help="processes for CANSLIM's and Trend Leaders' signals (default: all cores)")
    return parser.parse_args(argv)


def _screen_needs_key(args: argparse.Namespace) -> bool:
    """Only C/A/S/L screens, earnings gaps and statement-based scans call FMP; price-only scans read the cache."""
    if args.offline:
        return False
    if STRATEGIES[args.strategy].scan:
        from .scanning import SCANNERS
        return SCANNERS[args.strategy].needs_api
    return True


def _backfill(args: argparse.Namespace, settings: Settings, api_key: str) -> int:
    ctx = open_context(settings=settings, api_key=api_key)
    snapshot = None
    try:
        symbols = args.symbols.split(",") if args.symbols else None
        report = run_backfill(
            ctx.client, ctx.store, ctx.settings,
            symbols=symbols,
            sample=args.sample,
            refresh=not args.skip_universe,
            workers=args.workers,
        )
        if not symbols and not args.sample:
            # A full backfill also keeps the month's consensus estimates (estimate_snapshots.py), once a month.
            from .estimate_snapshots import take_if_due
            snapshot = take_if_due(ctx.store, ctx.client)
    except FMPPlanRestrictedError as exc:
        log.error("%s", exc)
        log.error(
            "Your FMP key does not include this endpoint. Check the plan on your FMP dashboard;"
            " --symbols runs on specific tickers without the universe call."
        )
        return 2
    except FMPAuthError as exc:
        log.error("%s", exc)
        return 2
    except (BackfillError, FMPError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        log.info("API calls this session: %d", ctx.client.stats.api_calls)
        ctx.close()
    print(report.format())
    if snapshot:
        print(f"Estimates snapshot {snapshot['month']}: "
              + (f"failed ({snapshot['failed']}); the next backfill retries it." if "failed" in snapshot else
                 f"{snapshot['estimates']} companies' estimates, {snapshot['float']} free floats, "
                 f"files {', '.join(snapshot['files']) or 'none'}"
                 + (f"; errors: {', '.join(snapshot['errors'])}" if snapshot["errors"] else "") + "."))
    return 0


def _estimates_snapshot(args: argparse.Namespace, settings: Settings, api_key: str) -> int:
    from .estimate_snapshots import due, label_for, take
    from datetime import date
    ctx = open_context(settings=settings, api_key=api_key)
    try:
        if not args.force and not due(ctx.store, date.today()):
            print(f"The {label_for(date.today())} snapshot is already saved; --force takes it again.")
            return 0
        print(json.dumps(take(ctx.store, ctx.client), indent=2))
    except FMPAuthError as exc:
        log.error("%s", exc)
        return 2
    except FMPError as exc:
        log.error("%s", exc)
        return 1
    finally:
        log.info("API calls this session: %d", ctx.client.stats.api_calls)
        ctx.close()
    return 0


def _status(settings: Settings) -> int:
    # No API key needed: this only reads the local database.
    store = Store(settings.data.db_path)
    limiter = SharedRateLimiter(settings.data.ratelimit_db_path, settings.fmp.calls_per_minute)
    try:
        summary = store.summary()
        summary["api_calls_today_utc_all_processes"] = limiter.calls_on()
        runs = store.last_runs("backfill", limit=1)
        if runs:
            run = runs[0]
            s = run["summary"] or {}
            summary["last_backfill"] = {
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "api_calls": s.get("api_calls"),
                "elapsed_seconds": round(s["elapsed_seconds"], 1) if s.get("elapsed_seconds") else None,
            }
        print(json.dumps(summary, indent=2))
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_settings(args.config)
        api_key = load_fmp_api_key() if args.command in {"backfill", "backtest-prepare", "backtest-capture", "backtest-delistings",
                                                          "backtest-approx-prepare", "estimates-snapshot"} or (args.command == "screen" and _screen_needs_key(args)) else None
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.data.log_dir, verbose=args.verbose, secrets=[api_key or ""])
    if args.command == "backfill":
        return _backfill(args, settings, api_key)
    if args.command == "estimates-snapshot":
        return _estimates_snapshot(args, settings, api_key)
    if args.command.startswith("backtest"):
        return _backtest_command(args, settings, api_key)
    if args.command == "strategy-backtest":
        return _strategy_backtest(args, settings)
    if args.command in ("web", "gui"):
        from dataclasses import replace
        from pathlib import Path
        from .web.app import main as web_main
        if args.db:
            settings = replace(settings, data=replace(settings.data, db_path=Path(args.db)))
        try:
            web_main(settings, port=args.port, host=args.host, show=args.open, public=args.public)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.command == "tradetest-bank":
        return _tradetest_bank(args, settings)
    if args.command == "publish":
        import sqlite3
        from .publish import PublishError, publish
        try:
            summary = publish(settings, args.output or settings.path.parent / "public" / "stratlib.db", years=args.years)
        except (PublishError, OSError, sqlite3.Error) as exc:
            log.error("%s", exc)
            return 1
        print(json.dumps(summary, indent=2))
        return 0
    if args.command == "screen":
        from .screening import ScreenError, run_screen
        ctx = open_context(settings=settings, api_key=api_key) if api_key else None
        store = ctx.store if ctx else Store(settings.data.db_path)
        try:
            report = run_screen(store, settings, ctx.client if ctx else None, sample=args.sample, strategy_id=args.strategy,
                                workers=args.workers or all_cores(),
                                progress=lambda message, done, total, calls: log.info(
                                    "%s %d/%d | API calls %d", message, done, total, calls))
            summary = {k: v for k, v in report.items() if k not in {"rows", "market", "candidates", "triggered", "blocked",
                                                                     "skipped", "rule_snapshot"}}
            summary["candidate_count"] = len(report.get("candidates", []))
            if "market" in report:
                summary["market_state"] = report["market"]["state"]
            print(json.dumps(summary, indent=2))
            return 0
        except (ScreenError, FMPError) as exc:
            log.error("%s", exc)
            return 1
        finally:
            store.close()
    return _status(settings)


def _tradetest_bank(args, settings) -> int:
    import sqlite3
    from pathlib import Path
    from .tradetest_bank import BankError, build_bank
    root = settings.path.parent
    last = [0.0]

    def note(message):
        if time.monotonic() - last[0] >= 5:
            last[0] = time.monotonic()
            log.info("%s", message)
    try:
        summary = build_bank(Path(args.db) if args.db else settings.data.db_path,
                             Path(args.output) if args.output else root / "public" / "tradetest_bank.npz",
                             windows=args.windows, seed=args.seed, progress=note,
                             etf_cache=Path(args.etf_cache) if args.etf_cache else root / "research" / "cache"
                             / "supply_demand_all_daily.json")
    except (BankError, OSError, sqlite3.Error, ValueError) as exc:
        log.error("%s", exc)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


def _strategy_backtest(args, settings) -> int:
    from .sim.runs import run_all
    store = Store(settings.data.db_path)
    try:
        results = run_all(store, settings, args.strategy, workers=args.workers or all_cores(),
                          progress=lambda message, done, total: log.info("%s %d/%d", message, done, total))
    except (ValueError, BacktestError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        store.close()
    rows = []
    for strategy_id, doc in results.items():
        combined = doc["results"]["combined"]
        rows.append({"strategy": doc["strategy"], "through": doc["data_through"], "trades": combined["trades"],
                     "cagr_pct": combined["cagr"], "spy_cagr_pct": combined["spy"]["cagr"],
                     "max_drawdown_pct": combined["max_drawdown"], "sharpe": combined["sharpe"]})
    print(json.dumps(rows, indent=2))
    return 0


def _backtest_command(args, settings, api_key):
    from dataclasses import replace
    from datetime import date
    from pathlib import Path
    from .backtest import BacktestError
    from . import backtest_approx
    from .backtest_data import capture_universe, coverage, import_archive, prepare_splits, refresh_delistings, run_backtest
    from .universe import parse_listings, sample_symbols

    ctx = open_context(settings=settings, api_key=api_key) if api_key else None
    store = ctx.store if ctx else Store(settings.data.db_path)
    try:
        if args.command == "backtest-import":
            result = import_archive(store, json.loads(Path(args.file).read_text(encoding="utf-8")))
        elif args.command == "backtest-capture":
            result = capture_universe(ctx.client, store, settings)
        elif args.command == "backtest-approx-prepare":
            last = [0.0]

            def note(message, done, total):
                if time.monotonic() - last[0] >= 5 or done == total:
                    last[0] = time.monotonic()
                    log.info("%s | API calls %d", message, ctx.client.stats.api_calls)
            result = backtest_approx.prepare(ctx.client, store, settings, args.start, args.end, progress=note)
        elif args.command == "backtest-rule-study":
            from .rule_study import format_study, run_study
            last = [0.0]

            def note(message, done, total):
                if time.monotonic() - last[0] >= 5 or done == total:
                    last[0] = time.monotonic()
                    log.info("%s | API calls 0", message)
            print(format_study(run_study(store, settings, args.start, args.end, progress=note)))
            return 0
        elif args.command == "backtest-delistings":
            result = refresh_delistings(ctx.client, store)
            result = {k: v for k, v in result.items() if k != "rows"} | {"records": len(result["rows"])}
        elif args.command == "backtest-prepare":
            start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            if start >= end or args.sample is not None and args.sample < 1:
                raise BacktestError("Choose an increasing date range and a positive sample.")
            snapshots = [v for v in store.vintages("universe") if args.start <= v["item"] <= args.end]
            if not snapshots:
                raise BacktestError("Import dated universe snapshots first; today's universe cannot select past members.")
            symbols = sorted({s.symbol for v in snapshots for s in parse_listings(v["body"]["stocks"])
                              if s.is_common and s.exchange in settings.universe.exchanges})
            if args.sample:
                symbols = sample_symbols(symbols, args.sample, settings.prices.sample_seed)
            years = max(settings.prices.history_years, date.today().year - start.year + 4)
            backfill_settings = replace(settings, prices=replace(settings.prices, history_years=years))
            backfill = run_backfill(ctx.client, store, backfill_settings, symbols=symbols, refresh=False)
            splits = prepare_splits(ctx.client, store, settings, args.start, args.end, sample=args.sample)
            delistings = refresh_delistings(ctx.client, store)
            result = {"prices": backfill.format(), "splits": splits, "delisted_records": len(delistings["rows"]),
                      "api_calls": ctx.client.stats.api_calls,
                      "note": "Import historical statement vintages; new FMP responses are observations made today."}
        elif args.check:
            result = (backtest_approx.check if args.approximate else coverage)(store, settings, args.start, args.end)
        else:
            options = settings.backtest
            if args.max_holdings is not None:
                options = replace(options, max_holdings=args.max_holdings)
            if args.capital is not None:
                options = replace(options, initial_capital=args.capital)
            if args.strategy != "breakout" and not args.approximate:
                raise BacktestError("The leaders and trend strategies need --approximate.")
            progress = lambda message, done, total: log.info("%s %d/%d | API calls 0", message, done, total)
            if args.approximate and args.strategy != "breakout":
                report = backtest_approx.run_leaders(store, settings, args.start, args.end, portfolio=options,
                                                     strategy=args.strategy, label=args.label, progress=progress,
                                                     workers=args.workers or all_cores())
            elif args.approximate:
                report = backtest_approx.run(store, settings, args.start, args.end, portfolio=options, sample=args.sample,
                                             label=args.label, progress=progress, workers=args.workers or all_cores())
            else:
                report = run_backtest(store, settings, args.start, args.end, portfolio=options, sample=args.sample,
                                      progress=lambda day, done, total: log.info("Backtest %s %d/%d | API calls 0", day, done, total))
            if args.output:
                Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
            result = {key: report.get(key) for key in ("start", "end", "strategy", "label", "metrics", "spy_metrics",
                                                         "signal_counts", "sample", "api_calls")}
        print(json.dumps(result, indent=2))
        return 1 if result.get("ready") is False else 0
    except (BacktestError, ConfigError, FMPError, BackfillError, ValueError, OSError, KeyError, TypeError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
