"""Local SQLite database: API response cache, stock universe, daily prices.

SQLite rather than DuckDB because the GUI and the CLI scripts need the same
file open at the same time, and DuckDB allows only one process to write.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .prices import Bar
from .sell_rules import Position, positive_price
from .sqlite_util import enable_wal
from .universe import ListedStock, exclusion_reason, one_listing_per_company

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    exchange TEXT,
    exchange_full TEXT,
    sector TEXT,
    industry TEXT,
    country TEXT,
    market_cap REAL,
    price REAL,            -- screener snapshot at last_seen
    avg_volume REAL,       -- screener snapshot at last_seen
    is_etf INTEGER NOT NULL DEFAULT 0,
    is_fund INTEGER NOT NULL DEFAULT 0,
    is_common INTEGER NOT NULL,
    excluded_reason TEXT,
    listed INTEGER NOT NULL DEFAULT 0,   -- present in the latest universe pull
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (symbol, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS price_state (
    symbol TEXT PRIMARY KEY,
    requested_from TEXT,   -- earliest start date requested from FMP
    first_date TEXT,
    last_date TEXT,
    last_close REAL,
    checked_at TEXT,       -- UTC time of the last fetch attempt
    status TEXT,           -- ok | empty | restricted | error
    error TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    args TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS screening_data (
    key TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL CHECK (entry_price > 0),
    breakout_date TEXT,
    breakout_price REAL,
    basis_date TEXT,
    basis_close REAL,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_vintages (
    kind TEXT NOT NULL,
    item TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, item, observed_at)
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class PriceState:
    symbol: str
    requested_from: str | None
    first_date: str | None
    last_date: str | None
    last_close: float | None
    checked_at: datetime | None
    status: str | None
    error: str | None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path, *, clock=time.time, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self._clock = clock
        self._lock = threading.RLock()
        if read_only:
            # A published snapshot: SQLite itself refuses every write, and nothing is created or migrated.
            if not self.db_path.is_file():
                raise FileNotFoundError(f"No database at {self.db_path}. Run stratlib publish to create a snapshot.")
            self._conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60, isolation_level=None,
                check_same_thread=False,
            )
            # Nothing can change a snapshot, so each document is parsed once and shared by every reader: a CANSLIM
            # screen is about 60 MB once parsed. Readers must not modify what they get.
            self.document = lru_cache(maxsize=16)(self.document)
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, timeout=60, isolation_level=None, check_same_thread=False
        )
        enable_wal(self._conn)
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # Additive migration: old entries keep their original CANSLIM behavior.
        # A missing snapshot is explicitly legacy, never silently frozen or reassigned.
        with self._transaction() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            for name, definition in {
                "strategy_id": "TEXT NOT NULL DEFAULT 'canslim'",
                "variant_id": "TEXT NOT NULL DEFAULT 'breakout'",
                "rule_snapshot": "TEXT", "quantity": "REAL", "stop_price": "REAL",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE positions ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _transaction(self):
        return _Transaction(self._conn, self._lock)

    # ------------------------------------------------------------------
    # Response cache (used by FMPClient)

    def get_response(self, key: str, max_age: timedelta) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, body FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or self._clock() - row[0] > max_age.total_seconds():
            return None
        return json.loads(row[1])

    def put_response(self, key: str, data: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO http_cache (key, fetched_at, body) VALUES (?, ?, ?)",
                (key, self._clock(), json.dumps(data, separators=(",", ":"))),
            )

    def clear_responses(self, prefix: str = "") -> int:
        """Drop cached responses whose key starts with ``prefix`` (all by default)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM http_cache WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # Universe

    def replace_universe(self, stocks: Sequence[ListedStock]) -> None:
        """Record the latest universe pull. Symbols missing from it stay in the
        table (their price history is kept) but are marked unlisted."""
        now = _utcnow_iso()
        with self._transaction() as conn:
            conn.execute("UPDATE symbols SET listed = 0")
            conn.executemany(
                """
                INSERT INTO symbols (symbol, name, exchange, exchange_full, sector, industry,
                    country, market_cap, price, avg_volume, is_etf, is_fund, is_common,
                    excluded_reason, listed, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name = excluded.name, exchange = excluded.exchange,
                    exchange_full = excluded.exchange_full, sector = excluded.sector,
                    industry = excluded.industry, country = excluded.country,
                    market_cap = excluded.market_cap, price = excluded.price,
                    avg_volume = excluded.avg_volume, is_etf = excluded.is_etf,
                    is_fund = excluded.is_fund, is_common = excluded.is_common,
                    excluded_reason = excluded.excluded_reason, listed = 1,
                    last_seen = excluded.last_seen
                """,
                [
                    (
                        s.symbol, s.name, s.exchange, s.exchange_full, s.sector, s.industry,
                        s.country, s.market_cap, s.price, s.avg_volume, int(s.is_etf), int(s.is_fund),
                        int(s.excluded_reason is None), s.excluded_reason, now, now,
                    )
                    for s in stocks
                ],
            )

    def reclassify_universe(self) -> int:
        """Re-apply the current common-stock rules to the stored listed symbols. No API call."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT symbol, name, exchange, exchange_full, sector, industry, country, market_cap, price,"
                " avg_volume, is_etf, is_fund, excluded_reason FROM symbols WHERE listed = 1"
            ).fetchall()
            before = {r[0]: r[12] for r in rows}
            stocks = one_listing_per_company([
                ListedStock(*r[:10], bool(r[10]), bool(r[11]),
                            exclusion_reason(r[0], r[1], exchange=r[2], is_etf=bool(r[10]), is_fund=bool(r[11])))
                for r in rows
            ])
            changed = [s for s in stocks if s.excluded_reason != before[s.symbol]]
            conn.executemany("UPDATE symbols SET is_common = ?, excluded_reason = ? WHERE symbol = ?",
                             [(int(s.excluded_reason is None), s.excluded_reason, s.symbol) for s in changed])
        return len(changed)

    def universe_symbols(self) -> list[str]:
        """Listed common stocks, sorted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM symbols WHERE listed = 1 AND is_common = 1 ORDER BY symbol"
            ).fetchall()
        return [r[0] for r in rows]

    def universe(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT symbol, name, exchange, sector, industry, last_seen FROM symbols"
                " WHERE listed = 1 AND is_common = 1 ORDER BY symbol"
            )
            columns = [col[0] for col in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def sectors(self) -> dict[str, tuple[str, str]]:
        """(sector, industry) for every stored symbol, listed or not."""
        with self._lock:
            rows = self._conn.execute("SELECT symbol, sector, industry FROM symbols").fetchall()
        return {symbol: (sector or "", industry or "") for symbol, sector, industry in rows}

    def save_document(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO screening_data VALUES (?, ?, ?)",
                (key, json.dumps(value, allow_nan=False), _utcnow_iso()),
            )

    def document(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM screening_data WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def document_keys(self, prefix: str) -> list[str]:
        """Keys starting with ``prefix``, newest update first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM screening_data WHERE substr(key, 1, ?) = ? ORDER BY updated_at DESC, key DESC",
                (len(prefix), prefix),
            ).fetchall()
        return [r[0] for r in rows]

    def delete_document(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM screening_data WHERE key = ?", (key,))

    def save_screen(self, report: dict) -> dict:
        """Publish a dated screen and its latest pointer atomically, scoped to a variant."""
        from uuid import uuid4
        from .strategies import snapshot_spec
        spec = snapshot_spec(report["rule_snapshot"])
        if report.get("strategy_id") != spec.id or report.get("variant_id") != spec.variant:
            raise ValueError("Screen metadata must match its saved strategy rules.")
        prefix = f"screen:{spec.id}:{spec.variant}:"
        history_key = f"{prefix}{report['price_date']}:{uuid4().hex}"
        saved = {**report, "screen_key": history_key}
        body, now = json.dumps(saved, allow_nan=False), _utcnow_iso()
        keys = [history_key, f"screen:latest:{spec.id}:{spec.variant}"]
        # This compatibility key remains CANSLIM-only for older CLI consumers.
        if spec.id == "canslim":
            keys.append("latest_screen")
        with self._transaction() as conn:
            for key in keys:
                conn.execute("INSERT OR REPLACE INTO screening_data VALUES (?, ?, ?)", (key, body, now))
        return saved

    def save_vintages(self, records: Sequence[dict]) -> None:
        """Append an archive batch atomically; an observation cannot be rewritten."""
        with self._transaction() as conn:
            for record in records:
                key = (record["kind"], record["item"], record["observed_at"])
                body = json.dumps(record["body"], sort_keys=True, allow_nan=False)
                old = conn.execute("SELECT body FROM research_vintages WHERE kind=? AND item=? AND observed_at=?", key).fetchone()
                if old is not None and json.loads(old[0]) != record["body"]:
                    raise ValueError("A dated research observation already exists with different contents.")
                conn.execute("INSERT OR IGNORE INTO research_vintages VALUES (?, ?, ?, ?)", (*key, body))

    def vintages(self, kind: str, item: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT item, observed_at, body FROM research_vintages WHERE kind=?"
                + (" AND item=?" if item is not None else "") + " ORDER BY observed_at",
                (kind, item) if item is not None else (kind,),
            ).fetchall()
        return [{"item": row[0], "observed_at": row[1], "body": json.loads(row[2])} for row in rows]

    def save_sponsorship(self, symbol: str, verdict: str, notes: str) -> None:
        if verdict not in {"Unreviewed", "Pass", "Fail"}:
            raise ValueError("Invalid sponsorship verdict")
        self.save_document(f"sponsorship:{symbol}", {
            "verdict": verdict, "notes": notes, "updated_at": _utcnow_iso(),
        })

    def sponsorship(self, symbol: str) -> dict[str, Any]:
        return self.document(f"sponsorship:{symbol}") or {"verdict": "Unreviewed", "notes": ""}

    def positions(self, *, closed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM positions WHERE closed_at IS " + ("NOT NULL" if closed else "NULL")
                + " ORDER BY entry_date DESC, id DESC"
            )
            columns = [column[0] for column in cur.description]
            records = [dict(zip(columns, row)) for row in cur.fetchall()]
            for record in records:
                record["rule_snapshot"] = json.loads(record["rule_snapshot"]) if record["rule_snapshot"] else None
            return records

    def save_position(self, position: Position, *, as_of: str, position_id: int | None = None,
                      tracking: dict | None = None) -> int:
        """Save a lot and the current price basis in one transaction. No API calls."""
        if position.entry_date > date.fromisoformat(as_of).isoformat():
            raise ValueError("Entry date cannot be in the future.")
        metadata = None
        if tracking is not None:
            from .strategies import snapshot_spec, strategy
            spec = strategy(tracking["strategy_id"])
            if tracking.get("variant_id") != spec.variant:
                raise ValueError("Unsupported position variant.")
            snapshot, quantity = tracking.get("rule_snapshot"), tracking.get("quantity")
            if snapshot is not None and snapshot_spec(snapshot) != spec:
                raise ValueError("Saved rules must match the position's strategy.")
            if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                                         or not math.isfinite(quantity) or quantity <= 0):
                raise ValueError("Shares must be positive, or left empty if unknown.")
            if spec.id != "canslim" and snapshot is None:
                raise ValueError("This strategy requires saved rules.")
            metadata = (spec.id, spec.variant, json.dumps(snapshot, allow_nan=False) if snapshot else None, quantity)
        now = _utcnow_iso()
        with self._transaction() as conn:
            anchor = conn.execute("SELECT date, close FROM prices WHERE symbol = ? AND date >= ? AND date <= ? "
                                  "ORDER BY date DESC LIMIT 1", (position.symbol, position.entry_date, as_of)).fetchone()
            basis = anchor if anchor and positive_price(anchor[1]) else (None, None)
            values = (position.symbol, position.entry_date, position.entry_price,
                      position.breakout_date, position.breakout_price, *basis, position.stop_price)
            if position_id is None:
                cur = conn.execute("INSERT INTO positions (symbol, entry_date, entry_price, breakout_date, "
                                   "breakout_price, basis_date, basis_close, stop_price, created_at, updated_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, now, now))
                position_id = cur.lastrowid
            else:
                cur = conn.execute("UPDATE positions SET symbol=?, entry_date=?, entry_price=?, breakout_date=?, "
                               "breakout_price=?, basis_date=?, basis_close=?, stop_price=?, updated_at=? "
                               "WHERE id=? AND closed_at IS NULL", (*values, now, position_id))
                if cur.rowcount != 1:
                    raise ValueError("This position is no longer open. Reload the page.")
            if metadata is not None:
                conn.execute("UPDATE positions SET strategy_id=?, variant_id=?, rule_snapshot=?, quantity=? WHERE id=?",
                             (*metadata, position_id))
            return position_id

    def set_position_closed(self, position_id: int, closed: bool) -> None:
        now = _utcnow_iso()
        with self._lock:
            cur = self._conn.execute("UPDATE positions SET closed_at=?, updated_at=? WHERE id=?",
                                     (now if closed else None, now, position_id))
            if cur.rowcount != 1:
                raise ValueError("Position was not found. Reload the page.")

    # ------------------------------------------------------------------
    # Prices

    def price_states(self, symbols: Iterable[str]) -> dict[str, PriceState]:
        wanted = set(symbols)
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, requested_from, first_date, last_date, last_close, checked_at,"
                " status, error FROM price_state"
            ).fetchall()
        states = {}
        for row in rows:
            if row[0] in wanted:
                checked = datetime.fromisoformat(row[5]) if row[5] else None
                states[row[0]] = PriceState(row[0], row[1], row[2], row[3], row[4], checked, *row[6:])
        return states

    def write_prices(
        self,
        symbol: str,
        bars: Sequence[Bar],
        *,
        replace: bool,
        requested_from: str | None,
        checked_at: datetime,
        status: str,
        error: str | None = None,
    ) -> int:
        """Store fetched bars and the symbol's fetch state in one transaction.

        ``replace`` drops the stored history first (after a restatement).
        ``requested_from`` of None leaves the recorded value unchanged.
        Returns the number of bars written.
        """
        with self._transaction() as conn:
            if replace:
                conn.execute("DELETE FROM prices WHERE symbol = ?", (symbol,))
            conn.executemany(
                "INSERT OR REPLACE INTO prices (symbol, date, open, high, low, close, volume)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(symbol, b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars],
            )
            first, last = conn.execute(
                "SELECT MIN(date), MAX(date) FROM prices WHERE symbol = ?", (symbol,)
            ).fetchone()
            last_close = None
            if last is not None:
                (last_close,) = conn.execute(
                    "SELECT close FROM prices WHERE symbol = ? AND date = ?", (symbol, last)
                ).fetchone()
            conn.execute(
                """
                INSERT INTO price_state (symbol, requested_from, first_date, last_date,
                    last_close, checked_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    requested_from = COALESCE(excluded.requested_from, price_state.requested_from),
                    first_date = excluded.first_date, last_date = excluded.last_date,
                    last_close = excluded.last_close, checked_at = excluded.checked_at,
                    status = excluded.status, error = excluded.error
                """,
                (
                    symbol, requested_from, first, last, last_close,
                    checked_at.isoformat(timespec="seconds"), status, error,
                ),
            )
        return len(bars)

    def price_rows(self, symbol: str, since: str, through: str) -> list[tuple]:
        """(date, high, close, volume) tuples, ascending, for bulk calculations."""
        with self._lock:
            return self._conn.execute(
                "SELECT date, high, close, volume FROM prices WHERE symbol = ? AND date >= ? AND date <= ?"
                " ORDER BY date", (symbol, since, through),
            ).fetchall()

    def price_panel_rows(self, symbols: Sequence[str], since: str, through: str) -> list[tuple]:
        """(symbol, date, open, high, low, close, volume) for many symbols, for building a panel.

        One indexed lookup per symbol inside a few statements; the whole universe loads in seconds.
        """
        rows: list[tuple] = []
        for start in range(0, len(symbols), 400):
            chunk = list(symbols[start:start + 400])
            with self._lock:
                rows += self._conn.execute(
                    "SELECT symbol, date, open, high, low, close, volume FROM prices WHERE symbol IN ("
                    + ",".join("?" * len(chunk)) + ") AND date >= ? AND date <= ?", (*chunk, since, through),
                ).fetchall()
        return rows

    def price_history(self, symbol: str, limit: int | None = None, through: str = "9999-12-31",
                      *, since: str = "0001-01-01") -> list[Bar]:
        """Read ascending bars within inclusive date bounds, optionally capped."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, open, high, low, close, volume FROM prices"
                " WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date DESC LIMIT ?",
                (symbol, since, through, limit if limit is not None else -1),
            ).fetchall()
        return [Bar(*r) for r in reversed(rows)]

    # ------------------------------------------------------------------
    # Run log

    def start_run(self, kind: str, args: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (kind, started_at, args) VALUES (?, ?, ?)",
                (kind, _utcnow_iso(), json.dumps(args)),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (_utcnow_iso(), json.dumps(summary), run_id),
            )

    def last_runs(self, kind: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, args, summary FROM runs"
                " WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [
            {
                "id": r[0],
                "started_at": r[1],
                "finished_at": r[2],
                "args": json.loads(r[3]) if r[3] else {},
                "summary": json.loads(r[4]) if r[4] else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Status

    def data_fingerprint(self) -> str:
        """A digest of the stored market data a backtest ranking reads: each symbol's price state, which every
        price write updates, the universe, and the backtest preparation documents. Any backfill, preparation or
        universe change alters it, so results saved under it are never reused on different data."""
        queries = (
            "SELECT symbol, requested_from, first_date, last_date, last_close, checked_at, status FROM price_state "
            "ORDER BY symbol",
            "SELECT * FROM symbols ORDER BY symbol",
            "SELECT key, updated_at, length(body) FROM screening_data WHERE (key >= 'backtest:approx:' AND "
            "key < 'backtest:approx;') OR (key >= 'backtest:splits:' AND key < 'backtest:splits;') OR "
            "key = 'backtest:delisted' ORDER BY key",
        )
        digest = hashlib.sha256()
        with self._lock:
            for sql in queries:
                for row in self._conn.execute(sql):
                    digest.update(repr(row).encode())
        return digest.hexdigest()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            q = self._conn.execute
            listed, common = q(
                "SELECT COUNT(*), COALESCE(SUM(is_common), 0) FROM symbols WHERE listed = 1"
            ).fetchone()
            with_prices, first, last = q(
                "SELECT COUNT(*), MIN(first_date), MAX(last_date) FROM price_state"
                " WHERE last_date IS NOT NULL"
            ).fetchone()
            (bars,) = q("SELECT COUNT(*) FROM prices").fetchone()
            statuses = dict(q("SELECT status, COUNT(*) FROM price_state GROUP BY status").fetchall())
        return {
            "listed_symbols": listed,
            "universe_common_stocks": common,
            "symbols_with_prices": with_prices,
            "price_bars": bars,
            "earliest_bar": first,
            "latest_bar": last,
            "price_status_counts": statuses,
        }


class _Transaction:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._lock.release()
