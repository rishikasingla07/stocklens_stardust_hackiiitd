"""Investigation history in SQLite (stdlib sqlite3): every Card produced is saved and can be reopened.

One short-lived connection per call, so the API worker threads and the live-job
thread can all use it safely. The database file is config.DB_PATH (git-ignored).
API keys are never stored: any key text is masked before a row is written.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from backend import config
from backend.models import IST, Card

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
SOURCES = ("investigate", "simulate", "live")
MODES = ("replay", "live")
MAX_LIMIT = 50
LIST_COLUMNS = "id, ticker, created_at, source, mode, label, score"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database (config.DB_PATH unless given), creating the file and table if missing."""
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    return conn


def init() -> None:
    """Create the database and table if missing (called on app startup)."""
    with closing(_connect()):
        pass


def _mask_keys(text: str) -> str:
    for key in (config.TINYFISH_API_KEY, config.LLM_API_KEY):
        if key and key.strip():
            text = text.replace(key.strip(), "***")
    return text


def save(card: Card, source: str, mode: str, path: Path | None = None) -> int:
    """Store one Card; returns its id. `path` pins the database (e.g. for a background job)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    card_json = _mask_keys(card.model_dump_json())
    with closing(_connect(path)) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO investigations (ticker, created_at, source, mode, label, score, card_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (card.ticker.upper(), datetime.now(IST).isoformat(timespec="seconds"), source, mode,
             card.confidence_label, card.confidence_score, card_json))
        return cursor.lastrowid


def list_history(ticker: str | None = None, limit: int = MAX_LIMIT) -> list[dict]:
    """Newest first, without card_json. Optionally only one ticker."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with closing(_connect()) as conn:
        if ticker:
            rows = conn.execute(
                f"SELECT {LIST_COLUMNS} FROM investigations WHERE ticker = ? ORDER BY id DESC LIMIT ?",
                (ticker.upper(), limit)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {LIST_COLUMNS} FROM investigations ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
    return [dict(row) for row in rows]


def get_card(entry_id: int) -> Card | None:
    """The stored Card for one history entry, or None if there is no such id."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT card_json FROM investigations WHERE id = ?", (entry_id,)).fetchone()
    return Card.model_validate(json.loads(row["card_json"])) if row else None
