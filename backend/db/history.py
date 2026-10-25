"""Investigation history in SQLite (stdlib sqlite3): every Card produced is saved and can be reopened.

History is per user: each row belongs to the user who produced it, and only
that user can list or reopen it. Connections come from backend/db/connection.py.
API keys are never stored: any key text is masked before a row is written.
"""

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from backend import config
from backend.db.connection import connect
from backend.models import IST, Card

SOURCES = ("investigate", "simulate", "live")
MODES = ("replay", "live")
MAX_LIMIT = 50
LIST_COLUMNS = "id, ticker, created_at, source, mode, label, score"

_connect = connect  # kept for callers of the old private name


def init() -> None:
    """Create the database and tables if missing (called on app startup)."""
    with closing(connect()):
        pass


def _mask_keys(text: str) -> str:
    for key in (config.TINYFISH_API_KEY, config.LLM_API_KEY):
        if key and key.strip():
            text = text.replace(key.strip(), "***")
    return text


def save(card: Card, source: str, mode: str, path: Path | None = None, *, user_id: int) -> int:
    """Store one Card for a user; returns its id. `path` pins the database (e.g. for a background job)."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    card_json = _mask_keys(card.model_dump_json())
    with closing(connect(path)) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO investigations (ticker, created_at, source, mode, label, score, card_json, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (card.ticker.upper(), datetime.now(IST).isoformat(timespec="seconds"), source, mode,
             card.confidence_label, card.confidence_score, card_json, user_id))
        return cursor.lastrowid


def list_history(user_id: int, ticker: str | None = None, limit: int = MAX_LIMIT) -> list[dict]:
    """One user's history, newest first, without card_json. Optionally only one ticker."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    with closing(connect()) as conn:
        if ticker:
            rows = conn.execute(
                f"SELECT {LIST_COLUMNS} FROM investigations WHERE user_id = ? AND ticker = ? "
                "ORDER BY id DESC LIMIT ?", (user_id, ticker.upper(), limit)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {LIST_COLUMNS} FROM investigations WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit)).fetchall()
    return [dict(row) for row in rows]


def get_card(entry_id: int, user_id: int) -> Card | None:
    """The stored Card for one of this user's history entries, or None (missing or someone else's)."""
    with closing(connect()) as conn:
        row = conn.execute("SELECT card_json FROM investigations WHERE id = ? AND user_id = ?",
                           (entry_id, user_id)).fetchone()
    return Card.model_validate(json.loads(row["card_json"])) if row else None
