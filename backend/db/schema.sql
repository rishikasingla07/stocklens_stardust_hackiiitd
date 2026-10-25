-- StockLens SQLite schema. Applied on app startup (and before first use) if missing.
-- No API keys and no plain-text passwords are ever stored.

-- One row per Card produced, owned by the user who produced it.
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,   -- ISO 8601 in IST, e.g. 2026-10-24T14:05:09+05:30
    source TEXT NOT NULL CHECK (source IN ('investigate', 'simulate', 'live')),
    mode TEXT NOT NULL CHECK (mode IN ('replay', 'live')),
    label TEXT NOT NULL,
    score INTEGER NOT NULL,
    card_json TEXT NOT NULL,    -- the full Card as JSON
    user_id INTEGER REFERENCES users (id) ON DELETE CASCADE  -- added by migration on older files
);

CREATE INDEX IF NOT EXISTS investigations_ticker_id ON investigations (ticker, id DESC);

-- Accounts. password_hash is hashlib.pbkdf2_hmac('sha256', password, salt, iterations), hex.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,         -- hex
    iterations INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Sessions. The cookie holds a random token; only its SHA-256 hash is stored here.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL    -- ISO 8601 in IST
);

CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);

-- Each user's watchlist, in the order they chose.
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (user_id, ticker)
);
