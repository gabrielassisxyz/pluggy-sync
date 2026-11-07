#!/usr/bin/env python3
"""Pull everything Pluggy will hand over into a local SQLite file.

Every row keeps the provider's JSON verbatim in `raw`. The extracted columns
beside it exist only so the common queries can use an index -- they are a
convenience over the raw document, never a replacement for it. Provider-side
enrichment (transaction categories above all) stops arriving when a plan
changes, and a field dropped at capture time cannot be refetched afterwards.

Stdlib only: no virtualenv, no install step, no lockfile to go stale. The HTTP
walk lives in the `pluggysync` package beside this file; this is the entry
point that maps its pages onto the tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

from pluggysync.lock import LockHeld, ProcessLock
from pluggysync.transport import PageAttempt
from pluggysync.walk import Client, FetchResult, utcnow

DB_PATH = os.environ.get(
    "PLUGGY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pluggy.db")
)


class AttemptCounter:
    """Stands in for the archive until the ledger lands: numbers the attempts,
    which is what the `api_calls` column has always counted."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, attempt: PageAttempt) -> int:
        self.count += 1
        return self.count


SCHEMA = """
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


def upsert(
    db: sqlite3.Connection, table: str, columns: dict[str, Any], row: dict[str, Any]
) -> None:
    columns = {
        **columns,
        "raw": json.dumps(row, ensure_ascii=False),
        "fetched_at": utcnow(),
    }
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{name}=excluded.{name}" for name in columns if name != "id")
    db.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
        list(columns.values()),
    )


def dig(row: dict[str, Any], *path: str) -> Any:
    """Reads a nested key, tolerating a missing level. Pluggy omits whole
    branches rather than sending them null."""
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def outcome_note(result: FetchResult) -> str:
    if result.outcome == "complete":
        return ""
    if result.outcome == "unavailable":
        return "unavailable"
    return f"incomplete: {result.error}"


def report(path: str, result: FetchResult) -> None:
    """A walk that stopped short is said out loud: silence here is what used
    to make a truncated history look like a short account."""
    if result.outcome == "incomplete":
        print(f"   ! {path}: incomplete ({result.error})", file=sys.stderr)


def sync() -> int:
    client_id = os.environ.get("PLUGGY_CLIENT_ID")
    client_secret = os.environ.get("PLUGGY_CLIENT_SECRET")
    item_ids = [
        i.strip() for i in (os.environ.get("PLUGGY_ITEM_IDS") or "").split(",") if i.strip()
    ]

    missing = [
        name
        for name, value in (
            ("PLUGGY_CLIENT_ID", client_id),
            ("PLUGGY_CLIENT_SECRET", client_secret),
            ("PLUGGY_ITEM_IDS", item_ids),
        )
        if not value
    ]
    if missing:
        print(
            f"missing in the environment: {', '.join(missing)} (see README: Running it)",
            file=sys.stderr,
        )
        return 2

    # Private files from here on: the database holds a person's financial history.
    os.umask(0o077)
    try:
        lock = ProcessLock.exclusive(DB_PATH)
    except LockHeld as held:
        print(f"{held}; refusing to run two syncs against one database", file=sys.stderr)
        return 1

    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    attempts = AttemptCounter()
    api = Client(client_id, client_secret, on_attempt=attempts)

    started = utcnow()
    cursor = db.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (started,))
    run_id = cursor.lastrowid

    def probe(path: str, result: FetchResult, rows: int) -> None:
        db.execute(
            "INSERT INTO probes (run_id, path, status, rows, note, fetched_at)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, path, result.status, rows, outcome_note(result), utcnow()),
        )
        report(path, result)

    print(f"database: {DB_PATH}")

    # The category tree is global rather than per-connection.
    categories = api.paged("/categories")
    category_rows = categories.documents
    for row in category_rows:
        upsert(
            db,
            "categories",
            {
                "id": row.get("id"),
                "description": row.get("description"),
                "parent_id": row.get("parentId"),
            },
            row,
        )
    probe("/categories", categories, len(category_rows))
    print(f"categories: {len(category_rows)}")

    for item_id in item_ids:
        print(f"\n── connection {item_id}")

        item_result = api.single(f"/items/{item_id}", expected_id=item_id)
        probe(f"/items/{item_id}", item_result, len(item_result.documents))
        if item_result.outcome != "complete":
            print(f"   ✗ HTTP {item_result.status}, skipped ({item_result.error})")
            continue
        item = item_result.documents[0]

        upsert(
            db,
            "items",
            {
                "id": item.get("id"),
                "connector": dig(item, "connector", "name"),
                "status": item.get("status"),
                "execution_status": item.get("executionStatus"),
                "last_updated": item.get("lastUpdatedAt"),
                "next_auto_sync": item.get("nextAutoSyncAt"),
            },
            item,
        )
        print(f"   {dig(item, 'connector', 'name')} — {item.get('status')}")

        consents = api.paged(f"/consents?itemId={item_id}")
        report(f"/consents?itemId={item_id}", consents)
        for row in consents.documents:
            upsert(
                db,
                "consents",
                {
                    "id": row.get("id"),
                    "item_id": item_id,
                    "expires_at": row.get("expiresAt"),
                    "revoked_at": row.get("revokedAt"),
                },
                row,
            )

        identity = api.single(f"/identity?itemId={item_id}")
        for row in identity.documents:
            upsert(
                db,
                "identities",
                {
                    "id": row.get("id"),
                    "item_id": item_id,
                    "document": row.get("document"),
                    "full_name": row.get("fullName"),
                },
                row,
            )
        probe(f"/identity?itemId={item_id}", identity, len(identity.documents))

        investments = api.paged(f"/investments?itemId={item_id}")
        investment_rows = investments.documents
        for row in investment_rows:
            upsert(
                db,
                "investments",
                {
                    "id": row.get("id"),
                    "item_id": item_id,
                    "type": row.get("type"),
                    "subtype": row.get("subtype"),
                    "name": row.get("name"),
                    "balance": row.get("balance"),
                    "currency": row.get("currencyCode"),
                },
                row,
            )
            movements = api.paged(f"/investments/{row.get('id')}/transactions")
            report(f"/investments/{row.get('id')}/transactions", movements)
            for tx in movements.documents:
                upsert(
                    db,
                    "investment_transactions",
                    {
                        "id": tx.get("id"),
                        "investment_id": row.get("id"),
                        "date": tx.get("date"),
                        "amount": tx.get("amount"),
                    },
                    tx,
                )
        probe(f"/investments?itemId={item_id}", investments, len(investment_rows))
        if investment_rows:
            print(f"   investments: {len(investment_rows)}")

        accounts = api.paged(f"/accounts?itemId={item_id}")
        probe(f"/accounts?itemId={item_id}", accounts, len(accounts.documents))

        for account in accounts.documents:
            account_id = account.get("id")
            upsert(
                db,
                "accounts",
                {
                    "id": account_id,
                    "item_id": item_id,
                    "type": account.get("type"),
                    "subtype": account.get("subtype"),
                    "name": account.get("name"),
                    "balance": account.get("balance"),
                    "currency": account.get("currencyCode"),
                },
                account,
            )

            transactions = api.cursored("/v2/transactions", f"?accountId={account_id}")
            transaction_rows = transactions.documents
            for row in transaction_rows:
                upsert(
                    db,
                    "transactions",
                    {
                        "id": row.get("id"),
                        "account_id": account_id,
                        "date": row.get("date"),
                        "description": row.get("description"),
                        "amount": row.get("amount"),
                        "currency": row.get("currencyCode"),
                        "type": row.get("type"),
                        "category": row.get("category"),
                    },
                    row,
                )
            probe(f"/v2/transactions?accountId={account_id}", transactions, len(transaction_rows))

            bills: list[dict[str, Any]] = []
            if account.get("type") == "CREDIT":
                bill_walk = api.paged(f"/bills?accountId={account_id}")
                bills = bill_walk.documents
                for row in bills:
                    upsert(
                        db,
                        "bills",
                        {
                            "id": row.get("id"),
                            "account_id": account_id,
                            "due_date": row.get("dueDate"),
                            "closing_date": row.get("closeDate") or row.get("closingDate"),
                            "total_amount": row.get("totalAmount"),
                            "minimum_payment": row.get("minimumPaymentAmount"),
                        },
                        row,
                    )
                probe(f"/bills?accountId={account_id}", bill_walk, len(bills))

            extra = f", {len(bills)} bills" if bills else ""
            print(f"   {account.get('name')}: {len(transaction_rows)} transactions{extra}")

        db.commit()

    db.execute(
        "UPDATE sync_runs SET finished_at=?, api_calls=? WHERE id=?",
        (utcnow(), attempts.count, run_id),
    )
    db.commit()

    print(f"\n{attempts.count} API calls")
    for table in (
        "items",
        "accounts",
        "transactions",
        "bills",
        "investments",
        "investment_transactions",
        "consents",
        "identities",
        "categories",
    ):
        count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:26} {count}")

    db.close()
    lock.release()
    return 0


if __name__ == "__main__":
    from pluggysync.cli import main

    sys.exit(main(sys.argv[1:]))
