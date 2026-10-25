"""One place that opens the SQLite database, applies schema.sql and runs small migrations.

One short-lived connection per call, so API worker threads and the live-job
thread can all use it safely. The database file is config.DB_PATH (git-ignored).
"""

import sqlite3
from pathlib import Path

from backend import config

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database file up to date. Safe to run every time."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(investigations)")}
    if "user_id" not in columns:
        # Rows saved before accounts existed have no owner, so no user sees them.
        conn.execute("ALTER TABLE investigations ADD COLUMN user_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS investigations_user_id ON investigations (user_id, id DESC)")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database (config.DB_PATH unless given), creating the file and tables if missing."""
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()
    return conn
