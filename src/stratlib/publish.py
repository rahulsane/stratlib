"""A read-only snapshot of the database for the public web app (``stratlib publish``).

The snapshot holds what the public pages read: the universe, saved screens, saved backtest results (only each
strategy's newest comparable backtest), cached statements, the market symbols' full price history and recent prices for every other stock. Positions, recorded
cash, sponsorship notes, Jev reviews, the run log, the API response cache and the backtest preparation data stay
behind. The file uses a rollback journal rather than WAL, so it is a single file that opens read-only anywhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .market_direction import INDEXES
from .store import Store

SNAPSHOT = "snapshot"
# Documents the public pages read, by exact key or key prefix. Everything else in screening_data stays private.
KEYS = ("latest_screen", "latest_backtest")
PREFIXES = ("screen:", "backtest:result:", "fundamentals:")
ENGINE = "backtest:engine:"   # comparable backtests: the newest per strategy is copied, since each run saves a new one
# Saved screen lists whose symbols need prices for their charts.
SCREEN_LISTS = ("rows", "candidates", "triggered", "blocked", "skipped")


class PublishError(ValueError):
    """The snapshot cannot be written."""


def publish(settings: Settings, output: str | Path, *, years: float = 4.0, now: datetime | None = None) -> dict:
    """Write the snapshot to ``output``, replacing any earlier one, and return what it holds."""
    source, output = Path(settings.data.db_path), Path(output)
    if not source.is_file():
        raise PublishError(f"No database at {source}. Run the backfill first.")
    if output.resolve() == source.resolve():
        raise PublishError("The snapshot cannot replace the working database. Choose another output path.")
    if years <= 0:
        raise PublishError("Years of prices must be positive.")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    partial.unlink(missing_ok=True)
    Store(partial).close()  # the schema, so every table the pages read exists, even empty ones
    conn = sqlite3.connect(partial.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("ATTACH DATABASE ? AS src", (source.resolve().as_uri() + "?mode=ro",))
        conn.execute("BEGIN")  # one read of the source, even while a backfill writes to it
        summary = _copy(conn, settings, years)
        summary["published_at"] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO screening_data VALUES (?, ?, ?)",
                     (SNAPSHOT, json.dumps(summary), summary["published_at"]))
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")
    except BaseException:
        conn.close()
        partial.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(partial, output)
    summary["path"] = str(output)
    summary["size_mb"] = round(output.stat().st_size / 1e6, 1)
    return summary


def _copy(conn: sqlite3.Connection, settings: Settings, years: float) -> dict:
    columns = ", ".join(row[1] for row in conn.execute("PRAGMA main.table_info(symbols)"))
    conn.execute(f"INSERT INTO main.symbols ({columns}) SELECT {columns} FROM src.symbols")
    placeholders = " OR ".join(["key = ?"] * len(KEYS) + ["substr(key, 1, ?) = ?"] * len(PREFIXES))
    values = [*KEYS, *[v for prefix in PREFIXES for v in (len(prefix), prefix)]]
    conn.execute(f"INSERT INTO main.screening_data SELECT key, body, updated_at FROM src.screening_data "
                 f"WHERE {placeholders}", values)
    # Keys are ENGINE + strategy + ":" + an ISO time, so the largest key per strategy is its newest run.
    conn.execute("INSERT INTO main.screening_data SELECT key, body, updated_at FROM src.screening_data WHERE key IN "
                 "(SELECT MAX(key) FROM src.screening_data WHERE substr(key, 1, ?) = ? "
                 "GROUP BY substr(key, 1, ? + instr(substr(key, ? + 1), ':')))",
                 (len(ENGINE), ENGINE, len(ENGINE), len(ENGINE)))

    (through,) = conn.execute("SELECT MAX(last_date) FROM src.price_state").fetchone()
    if through is None:
        raise PublishError("The database has no prices. Run the backfill first.")
    since = (date.fromisoformat(through) - timedelta(days=round(365.25 * years))).isoformat()
    # Market direction replays each index from its first session, so the market symbols keep their whole history.
    market = {*settings.prices.market_symbols, *INDEXES, "SPY", "QQQ"}
    stocks = {row[0] for row in conn.execute("SELECT symbol FROM src.symbols WHERE listed = 1 AND is_common = 1")}
    stocks |= _screen_symbols(conn)
    conn.execute("CREATE TEMP TABLE wanted (symbol TEXT PRIMARY KEY, whole INTEGER NOT NULL)")
    conn.executemany("INSERT INTO temp.wanted VALUES (?, ?)",
                     [(symbol, int(symbol in market)) for symbol in sorted(stocks | market)])
    copy = ("INSERT INTO main.prices SELECT p.symbol, p.date, p.open, p.high, p.low, p.close, p.volume "
            "FROM temp.wanted w JOIN src.prices p ON p.symbol = w.symbol")
    conn.execute(copy + " WHERE w.whole = 1")
    conn.execute(copy + " AND p.date >= ? WHERE w.whole = 0", (since,))

    screens = {}
    for key, body in conn.execute("SELECT key, body FROM main.screening_data WHERE substr(key, 1, 14) = 'screen:latest:'"
                                  " OR key = 'latest_screen'"):
        strategy = "canslim" if key == "latest_screen" else key.split(":")[2]
        screens.setdefault(strategy, json.loads(body).get("price_date"))

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]
    return {
        "prices_since": since, "prices_through": through,
        "symbols": count("SELECT COUNT(DISTINCT symbol) FROM main.prices"),
        "price_rows": count("SELECT COUNT(*) FROM main.prices"),
        "screens": dict(sorted(screens.items())),
        "backtests": count("SELECT COUNT(*) FROM main.screening_data WHERE substr(key, 1, 16) = 'backtest:result:'"),
        "comparable_backtests": count(f"SELECT COUNT(*) FROM main.screening_data WHERE substr(key, 1, {len(ENGINE)}) = '{ENGINE}'"),
        "statements": count("SELECT COUNT(*) FROM main.screening_data WHERE substr(key, 1, 13) = 'fundamentals:'"),
    }


def _screen_symbols(conn: sqlite3.Connection) -> set[str]:
    """Every symbol a copied screen lists, including stocks that have since left the universe."""
    symbols = set()
    for (body,) in conn.execute("SELECT body FROM main.screening_data WHERE substr(key, 1, 7) = 'screen:' "
                                "OR key = 'latest_screen'"):
        report = json.loads(body)
        for name in SCREEN_LISTS:
            symbols |= {item["symbol"] for item in report.get(name) or [] if isinstance(item, dict) and item.get("symbol")}
    return symbols
