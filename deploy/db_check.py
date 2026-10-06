"""Fold a SQLite database's write-ahead log into the file and check it. Prints "ok" when it is sound.

Used by deploy/db-sync.ps1 before an upload: python -I deploy/db_check.py data/stratlib.db
"""

import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
print(conn.execute("PRAGMA quick_check").fetchone()[0])
