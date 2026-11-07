"""The schema of the legacy collector, kept for what still reads it.

The legacy sync is gone: the ledger is the default write path since the
cutover. What remains of the old schema is the shape of the frozen legacy
databases, which the historical import reads, the promotion compares against
and the tests build their fixtures from.
"""

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  api_calls     INTEGER,
  note          TEXT
);

-- Every endpoint touched, whether it answered or refused. A 404 here is a
-- finding about the connector, not a failure of the run.
CREATE TABLE IF NOT EXISTS probes (
  run_id        INTEGER NOT NULL,
  path          TEXT NOT NULL,
  status        INTEGER,
  rows          INTEGER,
  note          TEXT,
  fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id            TEXT PRIMARY KEY,
  connector     TEXT, status TEXT, execution_status TEXT,
  last_updated  TEXT, next_auto_sync TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, type TEXT, subtype TEXT, name TEXT,
  balance       REAL, currency TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
  id            TEXT PRIMARY KEY,
  account_id    TEXT, date TEXT, description TEXT,
  amount        REAL, currency TEXT, type TEXT, category TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_by_date ON transactions(date DESC, id);
CREATE INDEX IF NOT EXISTS transactions_by_account ON transactions(account_id, date DESC);

CREATE TABLE IF NOT EXISTS bills (
  id            TEXT PRIMARY KEY,
  account_id    TEXT, due_date TEXT, closing_date TEXT,
  total_amount  REAL, minimum_payment REAL,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investments (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, type TEXT, subtype TEXT, name TEXT,
  balance       REAL, currency TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investment_transactions (
  id            TEXT PRIMARY KEY,
  investment_id TEXT, date TEXT, amount REAL,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, expires_at TEXT, revoked_at TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, document TEXT, full_name TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);

-- Pluggy's own category tree, worth a copy because it is the vocabulary the
-- transaction rows refer to.
CREATE TABLE IF NOT EXISTS categories (
  id            TEXT PRIMARY KEY,
  description   TEXT, parent_id TEXT,
  raw           TEXT NOT NULL, fetched_at TEXT NOT NULL
);
"""
