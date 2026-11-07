#!/usr/bin/env python3
"""Pull everything Pluggy will hand over into a local SQLite file.

Every row keeps the provider's JSON verbatim in `raw`. The extracted columns
beside it exist only so the common queries can use an index -- they are a
convenience over the raw document, never a replacement for it. Provider-side
enrichment (transaction categories above all) stops arriving when a plan
changes, and a field dropped at capture time cannot be refetched afterwards.

Stdlib only: no virtualenv, no install step, no lockfile to go stale.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

BASE = "https://api.pluggy.ai"
DB_PATH = os.environ.get(
    "PLUGGY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pluggy.db")
)

# Pluggy documents 360 requests/minute per IP; a prior implementation hardcoded
# 360/hour and the disagreement was never settled. Sequential requests with a
# small gap stay far below either reading, and batch processes against the API
# are explicitly prohibited, so there is nothing to gain by going faster.
REQUEST_GAP_SECONDS = 0.15


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Pluggy:
    """Thin API client: authenticates, renews on 401, waits out a 429."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._credentials = {"clientId": client_id, "clientSecret": client_secret}
        self._key: str | None = None
        self.calls = 0

    def _authenticate(self) -> str:
        body = json.dumps(self._credentials).encode()
        request = urllib.request.Request(
            f"{BASE}/auth",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["apiKey"]

    def key(self, force: bool = False) -> str:
        if self._key is None or force:
            self._key = self._authenticate()
        return self._key

    def get(self, path: str, retries: int = 3) -> tuple[int, Any]:
        """Returns (status, parsed body). A non-200 is a value, not an exception:
        several endpoints exist only for some connectors and answering "this one
        is not available here" is part of the survey."""
        url = path if path.startswith("http") else f"{BASE}{path}"

        for attempt in range(retries):
            time.sleep(REQUEST_GAP_SECONDS)
            self.calls += 1
            request = urllib.request.Request(url, headers={"X-API-KEY": self.key()})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                payload = error.read()
                if error.code == 401 and attempt < retries - 1:
                    self.key(force=True)
                    continue
                if error.code == 429 and attempt < retries - 1:
                    wait = int(error.headers.get("retry-after") or 60)
                    print(f"    429, waiting {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                try:
                    return error.code, json.loads(payload)
                except ValueError:
                    return error.code, {"body": payload.decode("utf8", "replace")}
            except urllib.error.URLError as error:
                if attempt < retries - 1:
                    time.sleep(2**attempt)
                    continue
                return 0, {"error": str(error)}
        return 0, {"error": "retries exhausted"}

    def paged(self, path: str) -> Iterator[dict[str, Any]]:
        """The page/totalPages style used by accounts, bills and investments."""
        joiner = "&" if "?" in path else "?"
        page = 1
        while True:
            status, body = self.get(f"{path}{joiner}pageSize=500&page={page}")
            if status != 200 or not isinstance(body, dict):
                return
            yield from body.get("results", [])
            if page >= int(body.get("totalPages") or 1):
                return
            page += 1

    def cursored(
        self, path: str, query: str, hop_limit: int = 500
    ) -> Iterator[dict[str, Any]]:
        """The cursor style used by /v2/transactions.

        `next` comes back as a query string relative to the endpoint
        (`?accountId=...&after=...`), so the path has to be prepended to it --
        sending it as a URL of its own silently hits the API root and ends the
        walk at whatever had already been read. Stops on a cursor that fails to
        advance, which would otherwise loop forever.
        """
        for hop in range(hop_limit):
            status, body = self.get(f"{path}{query}")
            if status != 200 or not isinstance(body, dict):
                # A mid-walk refusal truncates the history, and a truncation
                # that prints nothing is indistinguishable from a short account.
                print(
                    f"    ! walk cut short at {path} hop {hop}: HTTP {status}",
                    file=sys.stderr,
                )
                return
            yield from body.get("results", [])
            nxt = body.get("next")
            if not nxt or nxt == query:
                return
            query = nxt
        print(f"    ! {path} exceeded {hop_limit} pages", file=sys.stderr)


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


def sync() -> int:
    client_id = os.environ.get("PLUGGY_CLIENT_ID")
    client_secret = os.environ.get("PLUGGY_CLIENT_SECRET")
    item_ids = [
        i.strip()
        for i in (os.environ.get("PLUGGY_ITEM_IDS") or "").split(",")
        if i.strip()
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

    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    api = Pluggy(client_id, client_secret)

    started = utcnow()
    cursor = db.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (started,))
    run_id = cursor.lastrowid

    def probe(path: str, status: int, rows: int, note: str = "") -> None:
        db.execute(
            "INSERT INTO probes (run_id, path, status, rows, note, fetched_at) VALUES (?,?,?,?,?,?)",
            (run_id, path, status, rows, note, utcnow()),
        )

    print(f"database: {DB_PATH}")

    # The category tree is global rather than per-connection.
    status, body = api.get("/categories?pageSize=500")
    rows = body.get("results", []) if isinstance(body, dict) else []
    for row in rows:
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
    probe("/categories", status, len(rows))
    print(f"categories: {len(rows)}")

    for item_id in item_ids:
        print(f"\n── connection {item_id}")

        status, item = api.get(f"/items/{item_id}")
        probe(f"/items/{item_id}", status, 1 if status == 200 else 0)
        if status != 200:
            print(f"   ✗ HTTP {status}, skipped")
            continue

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

        for row in api.paged(f"/consents?itemId={item_id}"):
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

        status, identity = api.get(f"/identity?itemId={item_id}")
        if status == 200 and isinstance(identity, dict) and identity.get("id"):
            upsert(
                db,
                "identities",
                {
                    "id": identity.get("id"),
                    "item_id": item_id,
                    "document": identity.get("document"),
                    "full_name": identity.get("fullName"),
                },
                identity,
            )
        probe(f"/identity?itemId={item_id}", status, 1 if status == 200 else 0)

        investments = list(api.paged(f"/investments?itemId={item_id}"))
        for row in investments:
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
            for tx in api.paged(f"/investments/{row.get('id')}/transactions"):
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
        probe(f"/investments?itemId={item_id}", 200, len(investments))
        if investments:
            print(f"   investments: {len(investments)}")

        accounts = list(api.paged(f"/accounts?itemId={item_id}"))
        probe(f"/accounts?itemId={item_id}", 200, len(accounts))

        for account in accounts:
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

            transactions = list(
                api.cursored("/v2/transactions", f"?accountId={account_id}")
            )
            for row in transactions:
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
            probe(f"/v2/transactions?accountId={account_id}", 200, len(transactions))

            bills = []
            if account.get("type") == "CREDIT":
                bills = list(api.paged(f"/bills?accountId={account_id}"))
                for row in bills:
                    upsert(
                        db,
                        "bills",
                        {
                            "id": row.get("id"),
                            "account_id": account_id,
                            "due_date": row.get("dueDate"),
                            "closing_date": row.get("closeDate")
                            or row.get("closingDate"),
                            "total_amount": row.get("totalAmount"),
                            "minimum_payment": row.get("minimumPaymentAmount"),
                        },
                        row,
                    )
                probe(f"/bills?accountId={account_id}", 200, len(bills))

            extra = f", {len(bills)} bills" if bills else ""
            print(f"   {account.get('name')}: {len(transactions)} transactions{extra}")

        db.commit()

    db.execute(
        "UPDATE sync_runs SET finished_at=?, api_calls=? WHERE id=?",
        (utcnow(), api.calls, run_id),
    )
    db.commit()

    print(f"\n{api.calls} API calls")
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
    return 0


if __name__ == "__main__":
    sys.exit(sync())
