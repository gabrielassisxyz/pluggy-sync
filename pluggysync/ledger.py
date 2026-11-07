"""The ledger schema, its invariants, and how a database reaches the current version.

Three layers: `responses` archives every HTTP attempt's bytes; `documents`,
`versions` and `observations` record every sighting of every document version;
`events`, `ambiguities` and the projections are derived and rebuildable. History
is append-only by trigger, not by convention: an UPDATE or DELETE on a history
table aborts inside the database, whatever code issued it.

Local days are stored at ingest, computed with the zone rules in force at that
instant. A view-time `-3 hours` would reassign every historical day if daylight
saving came back.
"""

from __future__ import annotations

import filecmp
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_ZONE = ZoneInfo("America/Sao_Paulo")
BUSY_TIMEOUT_MS = 5000

KINDS = (
    "categories",
    "items",
    "consents",
    "identities",
    "investments",
    "accounts",
    "investment_transactions",
    "transactions",
    "bills",
    "loans",
)
_KIND_LIST = ", ".join(f"'{kind}'" for kind in KINDS)

# Append-only tables: no row is ever updated or deleted.
APPEND_ONLY_TABLES = (
    "responses",
    "page_derivations",
    "documents",
    "versions",
    "observations",
    "legacy_sync_runs",
    "legacy_probes",
    "cutover",
)


def local_day(instant: str) -> str:
    """The local calendar day of an ISO-8601 instant, under today's zone rules."""
    return datetime.fromisoformat(instant).astimezone(LOCAL_ZONE).date().isoformat()


def _append_only_triggers(table: str) -> list[str]:
    message = f"{table} is append-only"
    return [
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}"
        f" BEGIN SELECT RAISE(ABORT, '{message}'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}"
        f" BEGIN SELECT RAISE(ABORT, '{message}'); END",
    ]


SCHEMA_V1: list[str] = [
    """CREATE TABLE runs (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  started_day  TEXT NOT NULL,
  finished_at  TEXT,
  finished_day TEXT,
  outcome      TEXT NOT NULL CHECK (outcome IN
                 ('running','complete','partial','failed','interrupted','migrated')),
  api_calls    INTEGER,
  code_version TEXT NOT NULL,
  item_ids     TEXT NOT NULL CHECK (json_valid(item_ids)),
  note         TEXT,
  CHECK (outcome = 'running' OR (finished_at IS NOT NULL AND finished_day IS NOT NULL))
)""",
    f"""CREATE TABLE scopes (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES runs(id),
  kind          TEXT NOT NULL CHECK (kind IN ({_KIND_LIST})),
  parent        TEXT NOT NULL,
  expected      INTEGER NOT NULL CHECK (expected IN (0,1)),
  outcome       TEXT NOT NULL CHECK (outcome IN
                  ('pending','complete','incomplete','unavailable','skipped')),
  infer_absence INTEGER NOT NULL DEFAULT 0 CHECK (infer_absence IN (0,1)),
  status        INTEGER,
  rows          INTEGER,
  min_date      TEXT,
  min_instant   TEXT,
  closed_at     TEXT,
  closed_day    TEXT,
  error         TEXT,
  UNIQUE (run_id, kind, parent),
  CHECK (infer_absence = 0 OR outcome = 'complete'),
  CHECK ((kind = 'categories') = (parent = '')),
  CHECK (outcome = 'pending'
         OR (closed_at IS NOT NULL AND closed_day IS NOT NULL AND rows IS NOT NULL))
)""",
    """CREATE TABLE responses (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES runs(id),
  scope_id     INTEGER NOT NULL REFERENCES scopes(id),
  method       TEXT NOT NULL,
  url          TEXT NOT NULL,
  status       INTEGER NOT NULL CHECK (status >= 0),
  headers      TEXT CHECK (headers IS NULL OR json_type(headers) = 'array'),
  body_zlib    BLOB,
  body_sha256  TEXT,
  raw_bytes    INTEGER,
  truncated    INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0,1)),
  error        TEXT,
  received_at  TEXT NOT NULL,
  received_day TEXT NOT NULL,
  CHECK ((body_zlib IS NULL AND body_sha256 IS NULL AND raw_bytes IS NULL)
      OR (body_zlib IS NOT NULL AND body_sha256 IS NOT NULL
          AND raw_bytes IS NOT NULL AND raw_bytes >= 0)),
  CHECK (truncated = 0 OR error IS NOT NULL),
  CHECK (status > 0 OR error IS NOT NULL),
  CHECK (status > 0 OR body_zlib IS NULL)
)""",
    "CREATE INDEX responses_by_run ON responses(run_id, id)",
    "CREATE INDEX responses_by_scope ON responses(scope_id)",
    """CREATE TABLE page_derivations (
  response_id INTEGER PRIMARY KEY REFERENCES responses(id),
  valid       INTEGER NOT NULL CHECK (valid IN (0,1)),
  page_meta   TEXT,
  error       TEXT
)""",
    f"""CREATE TABLE documents (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ({_KIND_LIST})),
  provider_id TEXT NOT NULL CHECK (provider_id <> ''),
  UNIQUE (kind, provider_id)
)""",
    """CREATE TABLE versions (
  sha256 TEXT PRIMARY KEY,
  body   TEXT NOT NULL
)""",
    """CREATE TABLE observations (
  id           INTEGER PRIMARY KEY,
  run_id       INTEGER NOT NULL REFERENCES runs(id),
  scope_id     INTEGER NOT NULL REFERENCES scopes(id),
  response_id  INTEGER REFERENCES responses(id),
  position     INTEGER,
  document_id  INTEGER NOT NULL REFERENCES documents(id),
  parent       TEXT NOT NULL,
  sha256       TEXT NOT NULL REFERENCES versions(sha256),
  observed_at  TEXT NOT NULL,
  observed_day TEXT NOT NULL,
  UNIQUE (response_id, position),
  CHECK ((response_id IS NULL AND position IS NULL)
      OR (response_id IS NOT NULL AND position IS NOT NULL AND position >= 0))
)""",
    "CREATE INDEX observations_by_document ON observations(document_id, run_id, id)",
    "CREATE INDEX observations_by_run ON observations(run_id, document_id, id)",
    "CREATE INDEX observations_by_scope ON observations(scope_id)",
    """CREATE TABLE legacy_sync_runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, api_calls INTEGER, note TEXT
)""",
    """CREATE TABLE legacy_probes (
  seq INTEGER PRIMARY KEY, run_id INTEGER, path TEXT, status INTEGER,
  rows INTEGER, note TEXT, fetched_at TEXT
)""",
    """CREATE TABLE events (
  run_id        INTEGER NOT NULL REFERENCES runs(id),
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  seq           INTEGER NOT NULL,
  event         TEXT NOT NULL CHECK (event IN
                  ('appeared','changed','reappeared','moved','removed','parent_gone')),
  old_sha256    TEXT REFERENCES versions(sha256),
  new_sha256    TEXT REFERENCES versions(sha256),
  old_parent    TEXT,
  new_parent    TEXT,
  effective_at  TEXT NOT NULL,
  effective_day TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id, seq)
)""",
    "CREATE INDEX events_by_document ON events(document_id, run_id)",
    """CREATE TABLE ambiguities (
  run_id            INTEGER NOT NULL REFERENCES runs(id),
  document_id       INTEGER NOT NULL REFERENCES documents(id),
  candidate_parents TEXT NOT NULL CHECK (json_valid(candidate_parents)),
  reason            TEXT NOT NULL,
  PRIMARY KEY (run_id, document_id)
)""",
    """CREATE TABLE projection_state (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  projected_run INTEGER NOT NULL REFERENCES runs(id),
  code_version  TEXT NOT NULL
)""",
    """CREATE TABLE cutover (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  completed_at   TEXT NOT NULL,
  from_states    TEXT NOT NULL,
  acceptance_run INTEGER REFERENCES runs(id)
)""",
    *[statement for table in APPEND_ONLY_TABLES for statement in _append_only_triggers(table)],
    "CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs"
    " BEGIN SELECT RAISE(ABORT, 'runs rows are never deleted'); END",
    # A run closes once. Identity and provenance never change; the terminal
    # fields change only while the run is still running.
    """CREATE TRIGGER runs_close_once BEFORE UPDATE ON runs
WHEN OLD.outcome <> 'running'
  OR NEW.id IS NOT OLD.id
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.started_day IS NOT OLD.started_day
  OR NEW.code_version IS NOT OLD.code_version
  OR NEW.item_ids IS NOT OLD.item_ids
BEGIN
  SELECT RAISE(ABORT, 'runs: only the terminal fields may change, and only while running');
END""",
    "CREATE TRIGGER scopes_no_delete BEFORE DELETE ON scopes"
    " BEGIN SELECT RAISE(ABORT, 'scopes rows are never deleted'); END",
    """CREATE TRIGGER scopes_close_once BEFORE UPDATE ON scopes
WHEN OLD.outcome <> 'pending'
  OR NEW.id IS NOT OLD.id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.parent IS NOT OLD.parent
  OR NEW.expected IS NOT OLD.expected
BEGIN
  SELECT RAISE(ABORT, 'scopes: only the terminal fields may change, and only while pending');
END""",
]


def connect(path: str) -> sqlite3.Connection:
    """Autocommit off at the driver level: every transaction is an explicit
    BEGIN IMMEDIATE, and nothing here ever calls executescript()."""
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE / COMMIT, ROLLBACK on any exception. Inside an open
    transaction it joins it: the outer scope owns the commit."""
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


# -- migrations -------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    apply: Callable[[sqlite3.Connection], None]


def _create_version_1(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_V1:
        connection.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (Migration(1, _create_version_1),)
SCHEMA_VERSION = MIGRATIONS[-1].version


class LegacyDatabase(Exception):
    """The pre-ledger file. It is never migrated in place: the only path from it
    to the ledger is the import from the frozen states into a candidate."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} is a legacy pluggy-sync database and is never migrated in place;"
            " build the ledger with --candidate <path> --from-states ... and install it"
            " with --promote <path> --from-states ..."
        )


class UnknownDatabase(Exception):
    """Neither empty, nor legacy, nor a ledger file this code knows."""


class BackupConflict(Exception):
    """Something already sits at the backup path and is not the backup we would write."""


def user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def classify(path: str) -> str:
    """`new`, `legacy`, `ledger` or `unknown`, without writing a byte."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return "new"
    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        version = user_version(readonly)
        tables = {
            name
            for (name,) in readonly.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        readonly.close()
    if version >= 1:
        return "ledger"
    if not tables:
        return "new"
    if "sync_runs" in tables and "runs" not in tables:
        return "legacy"
    return "unknown"


def backup_path(path: str, version: int) -> str:
    return f"{path}-pre-v{version}"


def _snapshot_to_temporary(path: str) -> str:
    """A verified copy of the database in its own directory, mode 0600."""
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".backup-")
    os.close(fd)
    source = sqlite3.connect(path)
    copy = sqlite3.connect(temporary)
    try:
        source.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
        source.close()
    if verdict != "ok":
        os.unlink(temporary)
        raise BackupConflict(f"backup of {path} failed integrity_check: {verdict}")
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    return temporary


def write_backup(path: str, version: int, before_publish: Callable[[], None] | None = None) -> str:
    """Publishes `<path>-pre-v<version>` by exclusive create and never overwrites.

    An existing backup is reused only when it matches the copy we would write
    byte for byte: that is a failed earlier attempt that published before it
    committed. Anything else at that path aborts the migration.
    """
    target = backup_path(path, version)
    temporary = _snapshot_to_temporary(path)
    try:
        if os.path.exists(target):
            if filecmp.cmp(target, temporary, shallow=False):
                return target
            raise BackupConflict(f"{target} exists and differs from the current database")
        if before_publish is not None:
            before_publish()
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise BackupConflict(f"{target} appeared while the backup was being written") from None
    finally:
        os.unlink(temporary)
    directory = os.open(os.path.dirname(os.path.abspath(target)), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return target


def _apply(connection: sqlite3.Connection, migration: Migration) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        migration.apply(connection)
        connection.execute(f"PRAGMA user_version = {int(migration.version)}")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"foreign_key_check after v{migration.version}: {violations}"
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def open_ledger(
    path: str,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    before_publish: Callable[[], None] | None = None,
) -> sqlite3.Connection:
    """Opens a ledger database at the latest version, creating or migrating it.

    A brand-new file is created directly; a ledger file below the target is
    backed up (`write_backup`) and migrated one version at a time; a legacy
    file and an unknown file are refused untouched.
    """
    os.umask(0o077)
    state = classify(path)
    if state == "legacy":
        raise LegacyDatabase(path)
    if state == "unknown":
        raise UnknownDatabase(f"{path} is not empty and holds no schema this code knows")
    target = migrations[-1].version
    connection = connect(path)
    try:
        current = user_version(connection)
        if current > target:
            raise UnknownDatabase(f"{path} is at schema version {current}, newer than {target}")
        for migration in migrations:
            if migration.version <= current:
                continue
            if state == "ledger":
                write_backup(path, migration.version, before_publish)
            _apply(connection, migration)
    except BaseException:
        connection.close()
        raise
    os.chmod(path, 0o600)
    return connection
