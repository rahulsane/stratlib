"""Rate limiter shared by every thread and process through one SQLite file.

The CLI scripts and the GUI all build their limiter on the same file, so
running them at the same time cannot push the combined request rate past the
plan limit. Two rules apply to every call:

- at most ``max_calls`` calls in any sliding ``period`` (seconds), and
- consecutive calls at least ``period / max_calls`` apart, which spreads the
  budget evenly instead of spending it in a burst.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..sqlite_util import enable_wal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE TABLE IF NOT EXISTS daily_calls (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
"""


class SharedRateLimiter:
    def __init__(
        self,
        db_path: str | Path,
        max_calls: int,
        period: float = 60.0,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_calls < 1 or period <= 0:
            raise ValueError("max_calls must be >= 1 and period > 0")
        self.db_path = Path(db_path)
        self.max_calls = max_calls
        self.period = period
        self.min_interval = period / max_calls
        self._clock = clock
        self._sleep = sleep
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            enable_wal(conn)
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per call keeps this safe across threads and
        # processes; BEGIN IMMEDIATE serialises the check-and-record step.
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def acquire(self) -> float:
        """Block until a call is allowed, record it, and return its timestamp."""
        while True:
            recorded, wait = self._try_acquire()
            if recorded is not None:
                return recorded
            self._sleep(wait)

    def _try_acquire(self) -> tuple[float | None, float]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            conn.execute("DELETE FROM calls WHERE ts <= ?", (now - self.period,))
            count, oldest, newest = conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM calls"
            ).fetchone()
            wait = 0.0
            if count >= self.max_calls:
                wait = oldest + self.period - now
            if newest is not None:
                wait = max(wait, newest + self.min_interval - now)
            if wait > 0:
                conn.execute("ROLLBACK")
                return None, wait
            conn.execute("INSERT INTO calls (ts) VALUES (?)", (now,))
            conn.execute(
                "INSERT INTO daily_calls (day, calls) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET calls = calls + 1",
                (_utc_day(now),),
            )
            conn.execute("COMMIT")
            return now, 0.0
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def calls_in_window(self) -> int:
        """Calls made by all processes in the current sliding window."""
        conn = self._connect()
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM calls WHERE ts > ?", (self._clock() - self.period,)
            ).fetchone()
            return count
        finally:
            conn.close()

    def calls_on(self, day: str | None = None) -> int:
        """Calls made by all processes on a UTC day (default: today)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT calls FROM daily_calls WHERE day = ?", (day or _utc_day(self._clock()),)
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
