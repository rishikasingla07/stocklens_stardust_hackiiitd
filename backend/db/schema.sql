-- StockLens SQLite schema. Applied on app startup (and before first use) if missing.
-- One row per Card produced. No API keys are ever stored.

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,   -- ISO 8601 in IST, e.g. 2026-10-24T14:05:09+05:30
    source TEXT NOT NULL CHECK (source IN ('investigate', 'simulate', 'challenge', 'live')),
    mode TEXT NOT NULL CHECK (mode IN ('replay', 'live')),
    label TEXT NOT NULL,
    score INTEGER NOT NULL,
    card_json TEXT NOT NULL     -- the full Card as JSON
);

CREATE INDEX IF NOT EXISTS investigations_ticker_id ON investigations (ticker, id DESC);
