"""SQLite helpers shared by the store and the rate limiter."""

from __future__ import annotations

import random
import sqlite3
import time


def enable_wal(conn: sqlite3.Connection, attempts: int = 100) -> None:
    """Switch the database to WAL mode, retrying while another process holds it.

    The switch needs an exclusive lock and fails at once with "database is
    locked" instead of waiting on the busy timeout, which happens when the GUI
    and a script open a new database file at the same moment.
    """
    for attempt in range(attempts):
        try:
            (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            if mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.02 + random.random() * 0.05)
