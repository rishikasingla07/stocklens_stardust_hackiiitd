"""Accounts, sessions and watchlists in SQLite, stdlib only. Demo-grade auth, not production security.

- Passwords: hashlib.pbkdf2_hmac('sha256') with a random per-user salt; compared
  with hmac.compare_digest. The plain password is never stored, logged or returned.
- Sessions: secrets.token_urlsafe tokens with an expiry. The cookie carries the
  token; the database keeps only its SHA-256 hash, so a copied database file
  cannot be used to log in.
- Login gives the same answer for an unknown account and a wrong password, and
  does the same hashing work in both cases.
"""

import hashlib
import hmac
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend import config
from backend.db.connection import connect
from backend.models import IST


@dataclass(frozen=True)
class User:
    id: int
    account_id: str


class AccountExists(Exception):
    """Sign-up with an account ID that is already taken."""


def _now() -> datetime:
    return datetime.now(IST)


def hash_password(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# A fixed dummy so a login for an unknown account costs the same as a real check.
_DUMMY_SALT = b"stocklens-dummy-salt"


# --- Accounts ----------------------------------------------------------------

def create_user(account_id: str, password: str) -> User:
    """Create an account. Raises AccountExists if the ID is taken (case-insensitive)."""
    salt = secrets.token_bytes(config.AUTH_SALT_BYTES)
    iterations = config.AUTH_PBKDF2_ITERATIONS
    with closing(connect()) as conn, conn:
        if conn.execute("SELECT 1 FROM users WHERE account_id = ?", (account_id,)).fetchone():
            raise AccountExists(account_id)
        cursor = conn.execute(
            "INSERT INTO users (account_id, password_hash, salt, iterations, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, hash_password(password, salt, iterations), salt.hex(), iterations,
             _now().isoformat(timespec="seconds")))
        return User(cursor.lastrowid, account_id)


def find_user(account_id: str) -> User | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT id, account_id FROM users WHERE account_id = ?", (account_id,)).fetchone()
    return User(row["id"], row["account_id"]) if row else None


def verify_login(account_id: str, password: str) -> User | None:
    """The user if the ID and password match, else None. Same work and answer either way."""
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT id, account_id, password_hash, salt, iterations FROM users WHERE account_id = ?",
            (account_id,)).fetchone()
    if row is None:
        hash_password(password, _DUMMY_SALT, config.AUTH_PBKDF2_ITERATIONS)  # equalise timing
        return None
    candidate = hash_password(password, bytes.fromhex(row["salt"]), row["iterations"])
    if not hmac.compare_digest(candidate, row["password_hash"]):
        return None
    if row["account_id"].lower() == config.DEMO_ACCOUNT_ID.lower() and config.MODE != "replay":
        return None  # the published demo password only works in replay mode
    return User(row["id"], row["account_id"])


# --- Sessions ----------------------------------------------------------------

def create_session(user: User) -> str:
    """A new session token for the user (the raw token is returned once, never stored)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))  # tidy up
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user.id, now.isoformat(),
             (now + timedelta(hours=config.AUTH_SESSION_HOURS)).isoformat()))
    return token


def user_for_session(token: str | None) -> User | None:
    """The logged-in user for a session token, or None if missing, unknown or expired."""
    if not token:
        return None
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT users.id, users.account_id, sessions.expires_at FROM sessions "
            "JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ?",
            (_token_hash(token),)).fetchone()
    if row is None or datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    if row["account_id"].lower() == config.DEMO_ACCOUNT_ID.lower() and config.MODE != "replay":
        return None
    return User(row["id"], row["account_id"])


def delete_session(token: str | None) -> None:
    if not token:
        return
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


# --- Watchlists --------------------------------------------------------------

def get_watchlist(user_id: int) -> list[str]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY position",
                            (user_id,)).fetchall()
    return [row["ticker"] for row in rows]


def set_watchlist(user_id: int, tickers: list[str]) -> list[str]:
    """Replace the user's watchlist with these tickers (already validated), keeping their order."""
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM watchlist WHERE user_id = ?", (user_id,))
        conn.executemany("INSERT INTO watchlist (user_id, ticker, position) VALUES (?, ?, ?)",
                         [(user_id, t, i) for i, t in enumerate(tickers)])
    return list(tickers)


# --- Demo account ------------------------------------------------------------

def ensure_demo_account() -> bool:
    """Create demo / demo123 with a starting watchlist if missing. Returns True if it was created.

    Called at startup in replay mode only. An existing demo account is left as it is.
    """
    if find_user(config.DEMO_ACCOUNT_ID):
        return False
    try:
        user = create_user(config.DEMO_ACCOUNT_ID, config.DEMO_ACCOUNT_PASSWORD)
    except AccountExists:  # created by another worker at the same moment
        return False
    set_watchlist(user.id, list(config.DEMO_WATCHLIST))
    return True
