"""The nine current-state tables, projected from `document_state`.

They keep the legacy names, extracted columns and indexes so existing queries
keep working, and gain `first_seen_run`, `last_seen_run`, `state` and
`ambiguous_run`. Each row is a pure function of the document's state row and
the body it points at, so an incremental update after a run and a rebuild from
scratch are the same INSERT with and without a filter.
"""

from __future__ import annotations

import sqlite3

HISTORY_COLUMNS = """  raw            TEXT NOT NULL,
  fetched_at     TEXT NOT NULL,
  first_seen_run INTEGER NOT NULL,
  last_seen_run  INTEGER NOT NULL,
  state          TEXT NOT NULL CHECK (state IN ('present','removed','parent_gone')),
  ambiguous_run  INTEGER"""

PROJECTION_TABLES: list[str] = [
    f"""CREATE TABLE items (
  id            TEXT PRIMARY KEY,
  connector     TEXT, status TEXT, execution_status TEXT,
  last_updated  TEXT, next_auto_sync TEXT,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE accounts (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, type TEXT, subtype TEXT, name TEXT,
  balance       REAL, currency TEXT,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE transactions (
  id            TEXT PRIMARY KEY,
  account_id    TEXT, date TEXT, description TEXT,
  amount        REAL, currency TEXT, type TEXT, category TEXT,
{HISTORY_COLUMNS}
)""",
    "CREATE INDEX transactions_by_date ON transactions(date DESC, id)",
    "CREATE INDEX transactions_by_account ON transactions(account_id, date DESC)",
    f"""CREATE TABLE bills (
  id            TEXT PRIMARY KEY,
  account_id    TEXT, due_date TEXT, closing_date TEXT,
  total_amount  REAL, minimum_payment REAL,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE investments (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, type TEXT, subtype TEXT, name TEXT,
  balance       REAL, currency TEXT,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE investment_transactions (
  id            TEXT PRIMARY KEY,
  investment_id TEXT, date TEXT, amount REAL,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE consents (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, expires_at TEXT, revoked_at TEXT,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE identities (
  id            TEXT PRIMARY KEY,
  item_id       TEXT, document TEXT, full_name TEXT,
{HISTORY_COLUMNS}
)""",
    f"""CREATE TABLE categories (
  id            TEXT PRIMARY KEY,
  description   TEXT, parent_id TEXT,
{HISTORY_COLUMNS}
)""",
]

PROJECTION_NAMES = (
    "items",
    "accounts",
    "transactions",
    "bills",
    "investments",
    "investment_transactions",
    "consents",
    "identities",
    "categories",
)


def _json(path: str) -> str:
    return f"json_extract(v.body, '$.{path}')"


# Extracted columns per kind. Parent columns come from the sighting's parent,
# never from the body: bills and investment movements carry no parent at all.
EXTRACTED: dict[str, list[tuple[str, str]]] = {
    "items": [
        ("connector", _json("connector.name")),
        ("status", _json("status")),
        ("execution_status", _json("executionStatus")),
        ("last_updated", _json("lastUpdatedAt")),
        ("next_auto_sync", _json("nextAutoSyncAt")),
    ],
    "accounts": [
        ("item_id", "ds.parent"),
        ("type", _json("type")),
        ("subtype", _json("subtype")),
        ("name", _json("name")),
        ("balance", _json("balance")),
        ("currency", _json("currencyCode")),
    ],
    "transactions": [
        ("account_id", "ds.parent"),
        ("date", _json("date")),
        ("description", _json("description")),
        ("amount", _json("amount")),
        ("currency", _json("currencyCode")),
        ("type", _json("type")),
        ("category", _json("category")),
    ],
    "bills": [
        ("account_id", "ds.parent"),
        ("due_date", _json("dueDate")),
        ("closing_date", f"coalesce({_json('billClosingDate')}, {_json('closeDate')})"),
        ("total_amount", _json("totalAmount")),
        ("minimum_payment", _json("minimumPaymentAmount")),
    ],
    "investments": [
        ("item_id", "ds.parent"),
        ("type", _json("type")),
        ("subtype", _json("subtype")),
        ("name", _json("name")),
        ("balance", _json("balance")),
        ("currency", _json("currencyCode")),
    ],
    "investment_transactions": [
        ("investment_id", "ds.parent"),
        ("date", _json("date")),
        ("amount", _json("amount")),
    ],
    "consents": [
        ("item_id", "ds.parent"),
        ("expires_at", _json("expiresAt")),
        ("revoked_at", _json("revokedAt")),
    ],
    "identities": [
        ("item_id", "ds.parent"),
        ("document", _json("document")),
        ("full_name", _json("fullName")),
    ],
    "categories": [
        ("description", _json("description")),
        ("parent_id", _json("parentId")),
    ],
}


def _projection_sql(kind: str, where: str) -> str:
    names = ", ".join(name for name, _ in EXTRACTED[kind])
    expressions = ", ".join(expression for _, expression in EXTRACTED[kind])
    return (
        f"INSERT OR REPLACE INTO {kind} (id, {names}, raw, fetched_at, first_seen_run,"
        " last_seen_run, state, ambiguous_run)"
        f" SELECT d.provider_id, {expressions}, v.body, ds.observed_at, ds.first_seen_run,"
        " ds.last_seen_run, ds.state, ds.ambiguous_run"
        " FROM document_state ds JOIN documents d ON d.id = ds.document_id"
        " JOIN versions v ON v.sha256 = ds.sha256"
        f" WHERE d.kind = '{kind}'{where}"
    )


def project_documents(connection: sqlite3.Connection, run_id: int) -> None:
    """Refreshes the rows of every document the run sighted or changed the state of."""
    for kind in PROJECTION_NAMES:
        connection.execute(
            _projection_sql(kind, " AND (ds.last_seen_run = ? OR ds.state_run = ?)"),
            (run_id, run_id),
        )


def rebuild_projection_tables(connection: sqlite3.Connection) -> None:
    for kind in PROJECTION_NAMES:
        connection.execute(f"DELETE FROM {kind}")
        connection.execute(_projection_sql(kind, ""))
