"""The ledger schema and the invariants the database enforces on its own:
append-only history, closed-once runs and scopes, and every CHECK the plan names."""

from __future__ import annotations

import filecmp
import itertools
import os
import shutil
import sqlite3
import tempfile
import unittest

from pluggysync.ledger import (
    APPEND_ONLY_TABLES,
    KINDS,
    LegacyDatabase,
    UnknownDatabase,
    classify,
    local_day,
    open_ledger,
    user_version,
)
from pluggysync.legacy_schema import LEGACY_SCHEMA

EXPECTED_TABLES = {
    "runs",
    "scopes",
    "responses",
    "page_derivations",
    "documents",
    "versions",
    "observations",
    "ambiguities",
    "events",
    "projection_state",
    "cutover",
    "legacy_sync_runs",
    "legacy_probes",
    "document_state",
    "items",
    "accounts",
    "transactions",
    "bills",
    "investments",
    "investment_transactions",
    "consents",
    "identities",
    "categories",
}
EXPECTED_INDEXES = {
    "document_state_by_parent",
    "transactions_by_date",
    "transactions_by_account",
    "observations_by_document",
    "observations_by_run",
    "observations_by_scope",
    "responses_by_scope",
    "responses_by_run",
    "events_by_document",
}

INSTANT = "2026-01-05T03:00:00+00:00"
DAY = "2026-01-05"


def insert_run(db: sqlite3.Connection, outcome: str = "running") -> int:
    finished = (INSTANT, DAY) if outcome != "running" else (None, None)
    cursor = db.execute(
        "INSERT INTO runs (started_at, started_day, finished_at, finished_day, outcome,"
        " code_version, item_ids) VALUES (?,?,?,?,?,?,?)",
        (INSTANT, DAY, *finished, outcome, "test", '["item-0001"]'),
    )
    return int(cursor.lastrowid or 0)


def insert_scope(
    db: sqlite3.Connection,
    run_id: int,
    kind: str = "accounts",
    parent: str = "item-0001",
    outcome: str = "pending",
    **overrides: object,
) -> int:
    row: dict[str, object] = {
        "run_id": run_id,
        "kind": kind,
        "parent": parent,
        "expected": 1,
        "outcome": outcome,
        "infer_absence": 0,
    }
    if outcome != "pending":
        row.update(closed_at=INSTANT, closed_day=DAY, rows=0)
    row.update(overrides)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cursor = db.execute(f"INSERT INTO scopes ({names}) VALUES ({marks})", list(row.values()))
    return int(cursor.lastrowid or 0)


def insert_response(db: sqlite3.Connection, run_id: int, scope_id: int, **overrides: object) -> int:
    row: dict[str, object] = {
        "run_id": run_id,
        "scope_id": scope_id,
        "method": "GET",
        "url": "https://api.example/accounts?itemId=item-0001&pageSize=500&page=1",
        "status": 200,
        "headers": '[["content-type","application/json"]]',
        "body_zlib": b"x",
        "body_sha256": "0" * 64,
        "raw_bytes": 2,
        "truncated": 0,
        "error": None,
        "received_at": INSTANT,
        "received_day": DAY,
    }
    row.update(overrides)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cursor = db.execute(f"INSERT INTO responses ({names}) VALUES ({marks})", list(row.values()))
    return int(cursor.lastrowid or 0)


_fresh = itertools.count(1)


def insert_document(db: sqlite3.Connection, kind: str = "accounts") -> int:
    provider_id = f"doc-{next(_fresh):04d}"
    cursor = db.execute(
        "INSERT INTO documents (kind, provider_id) VALUES (?,?)", (kind, provider_id)
    )
    return int(cursor.lastrowid or 0)


def insert_version(db: sqlite3.Connection) -> str:
    sha = f"{next(_fresh):064x}"
    db.execute("INSERT INTO versions (sha256, body) VALUES (?,?)", (sha, '{"id":"a-1"}'))
    return sha


def insert_observation(
    db: sqlite3.Connection, run_id: int, scope_id: int, response_id: int | None, **overrides: object
) -> int:
    row: dict[str, object] = {
        "run_id": run_id,
        "scope_id": scope_id,
        "response_id": response_id,
        "position": 0 if response_id is not None else None,
        "document_id": insert_document(db),
        "parent": "item-0001",
        "sha256": insert_version(db),
        "observed_at": INSTANT,
        "observed_day": DAY,
    }
    row.update(overrides)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cursor = db.execute(f"INSERT INTO observations ({names}) VALUES ({marks})", list(row.values()))
    return int(cursor.lastrowid or 0)


def populate(db: sqlite3.Connection) -> dict[str, int]:
    """One valid row in every append-only table, so each trigger has a target."""
    run_id = insert_run(db)
    scope_id = insert_scope(db, run_id)
    response_id = insert_response(db, run_id, scope_id)
    db.execute(
        "INSERT INTO page_derivations (response_id, valid, page_meta) VALUES (?,?,?)",
        (response_id, 1, '{"page":1,"totalPages":1}'),
    )
    insert_observation(db, run_id, scope_id, response_id)
    db.execute("INSERT INTO legacy_sync_runs (id, started_at) VALUES (1, ?)", (INSTANT,))
    db.execute(
        "INSERT INTO legacy_probes (seq, run_id, path, status, rows, fetched_at)"
        " VALUES (1, 1, '/categories', 200, 130, ?)",
        (INSTANT,),
    )
    db.execute(
        "INSERT INTO cutover (id, completed_at, from_states) VALUES (1, ?, '[]')", (INSTANT,)
    )
    return {"run": run_id, "scope": scope_id, "response": response_id}


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "ledger.db")
        self.db = open_ledger(self.path)
        self.addCleanup(self.db.close)

    def assertRefused(self, sql: str, *parameters: object) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, parameters)


class SchemaCreationTest(LedgerCase):
    def test_new_file_has_every_table_trigger_and_index_at_version_1(self) -> None:
        rows = self.db.execute("SELECT type, name FROM sqlite_master").fetchall()
        tables = {name for kind, name in rows if kind == "table" and not name.startswith("sqlite_")}
        indexes = {
            name for kind, name in rows if kind == "index" and not name.startswith("sqlite_")
        }
        triggers = {name for kind, name in rows if kind == "trigger"}

        self.assertEqual(tables, EXPECTED_TABLES)
        self.assertEqual(indexes, EXPECTED_INDEXES)
        for table in APPEND_ONLY_TABLES:
            self.assertIn(f"{table}_no_update", triggers)
            self.assertIn(f"{table}_no_delete", triggers)
        self.assertTrue(
            {"runs_no_delete", "runs_close_once", "scopes_no_delete", "scopes_close_once"}
            <= triggers
        )
        self.assertEqual(user_version(self.db), 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_connection_has_foreign_keys_on(self) -> None:
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_scope(self.db, run_id=999)

    def test_kinds_match_the_scope_vocabulary(self) -> None:
        run_id = insert_run(self.db)
        for kind in KINDS:
            parent = "" if kind == "categories" else "p"
            insert_scope(self.db, run_id, kind=kind, parent=parent)
        self.assertRefused(
            "INSERT INTO scopes (run_id, kind, parent, expected, outcome) VALUES (?,?,?,?,?)",
            run_id,
            "balances",
            "p",
            1,
            "pending",
        )

    def test_local_day_uses_sao_paulo_rules(self) -> None:
        self.assertEqual(local_day("2026-01-05T02:59:00+00:00"), "2026-01-04")
        self.assertEqual(local_day("2026-01-05T03:00:00+00:00"), "2026-01-05")


class AppendOnlyTest(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("BEGIN IMMEDIATE")
        self.ids = populate(self.db)
        self.db.execute("COMMIT")

    def test_update_and_delete_are_refused_on_every_append_only_table(self) -> None:
        for table in APPEND_ONLY_TABLES:
            column = self.db.execute(f"PRAGMA table_info({table})").fetchall()[-1][1]
            with self.subTest(table=table, verb="UPDATE"):
                self.assertRefused(f"UPDATE {table} SET {column} = {column}")
            with self.subTest(table=table, verb="DELETE"):
                self.assertRefused(f"DELETE FROM {table}")

    def test_runs_and_scopes_are_never_deleted(self) -> None:
        self.assertRefused("DELETE FROM runs")
        self.assertRefused("DELETE FROM scopes")

    def test_a_running_run_closes_once(self) -> None:
        self.db.execute(
            "UPDATE runs SET outcome='complete', finished_at=?, finished_day=?, api_calls=3"
            " WHERE id=?",
            (INSTANT, DAY, self.ids["run"]),
        )
        self.assertRefused("UPDATE runs SET outcome='running', finished_at=NULL, finished_day=NULL")
        self.assertRefused("UPDATE runs SET note='late' WHERE id=?", self.ids["run"])

    def test_run_identity_and_provenance_never_change(self) -> None:
        self.assertRefused("UPDATE runs SET code_version='other' WHERE id=?", self.ids["run"])
        self.assertRefused("UPDATE runs SET item_ids='[]' WHERE id=?", self.ids["run"])
        self.assertRefused("UPDATE runs SET started_at='2000-01-01T00:00:00+00:00'")

    def test_a_pending_scope_closes_once_and_keeps_its_identity(self) -> None:
        self.assertRefused("UPDATE scopes SET kind='bills' WHERE id=?", self.ids["scope"])
        self.assertRefused("UPDATE scopes SET parent='other' WHERE id=?", self.ids["scope"])
        self.db.execute(
            "UPDATE scopes SET outcome='complete', rows=1, closed_at=?, closed_day=?, status=200"
            " WHERE id=?",
            (INSTANT, DAY, self.ids["scope"]),
        )
        self.assertRefused("UPDATE scopes SET rows=2 WHERE id=?", self.ids["scope"])

    def test_inserting_an_already_closed_row_is_allowed(self) -> None:
        insert_run(self.db, outcome="migrated")
        insert_scope(self.db, self.ids["run"], kind="bills", parent="acc-1", outcome="complete")


class CheckConstraintTest(LedgerCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_id = insert_run(self.db)
        self.scope_id = insert_scope(self.db, self.run_id)

    def test_infer_absence_needs_a_complete_scope(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_scope(
                self.db,
                self.run_id,
                kind="bills",
                parent="acc-1",
                outcome="incomplete",
                infer_absence=1,
            )
        insert_scope(
            self.db, self.run_id, kind="bills", parent="acc-1", outcome="complete", infer_absence=1
        )

    def test_categories_is_the_only_global_kind(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_scope(self.db, self.run_id, kind="categories", parent="x")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_scope(self.db, self.run_id, kind="bills", parent="")
        insert_scope(self.db, self.run_id, kind="categories", parent="")

    def test_a_closed_scope_needs_its_terminal_fields(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_scope(
                self.db, self.run_id, kind="bills", parent="acc-1", outcome="complete", rows=None
            )

    def test_truncated_needs_an_error(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, truncated=1, error=None)
        insert_response(self.db, self.run_id, self.scope_id, truncated=1, error="truncated")

    def test_status_0_needs_an_error_and_has_no_body(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, status=0, error=None)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(
                self.db, self.run_id, self.scope_id, status=0, error="no response", body_zlib=b"x"
            )
        insert_response(
            self.db,
            self.run_id,
            self.scope_id,
            status=0,
            error="no response",
            body_zlib=None,
            body_sha256=None,
            raw_bytes=None,
            headers=None,
        )

    def test_body_columns_are_all_present_or_all_absent(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, body_sha256=None)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, raw_bytes=-1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, raw_bytes=None)

    def test_headers_must_be_a_json_array(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            insert_response(self.db, self.run_id, self.scope_id, headers='{"a":1}')

    def test_a_live_observation_has_both_response_and_position(self) -> None:
        response_id = insert_response(self.db, self.run_id, self.scope_id)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_observation(self.db, self.run_id, self.scope_id, response_id, position=None)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_observation(self.db, self.run_id, self.scope_id, None, position=0)
        insert_observation(self.db, self.run_id, self.scope_id, None)
        insert_observation(self.db, self.run_id, self.scope_id, response_id, position=0)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_observation(self.db, self.run_id, self.scope_id, response_id, position=0)

    def test_terminal_run_needs_finished_fields(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO runs (started_at, started_day, outcome, code_version, item_ids)"
                " VALUES (?,?,?,?,?)",
                (INSTANT, DAY, "complete", "test", "[]"),
            )

    def test_event_kinds_and_singletons(self) -> None:
        document_id = insert_document(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO events (run_id, document_id, seq, event, effective_at, effective_day)"
                " VALUES (?,?,?,?,?,?)",
                (self.run_id, document_id, 1, "vanished", INSTANT, DAY),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO projection_state (id, projected_run, code_version) VALUES (2, ?, 'x')",
                (self.run_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO cutover (id, completed_at, from_states) VALUES (2, ?, '[]')",
                (INSTANT,),
            )


class LegacyRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.legacy = os.path.join(self.directory.name, "pluggy.db")
        db = sqlite3.connect(self.legacy)
        db.executescript(LEGACY_SCHEMA)
        db.execute(
            "INSERT INTO sync_runs (started_at, finished_at, api_calls) VALUES (?,?,?)",
            (INSTANT, INSTANT, 40),
        )
        db.commit()
        db.close()

    def test_legacy_file_is_refused_and_left_byte_identical(self) -> None:
        frozen = os.path.join(self.directory.name, "frozen.db")
        shutil.copyfile(self.legacy, frozen)

        self.assertEqual(classify(self.legacy), "legacy")
        with self.assertRaises(LegacyDatabase) as refused:
            open_ledger(self.legacy)
        self.assertIn("--candidate", str(refused.exception))
        self.assertIn("--promote", str(refused.exception))
        self.assertTrue(filecmp.cmp(self.legacy, frozen, shallow=False))
        self.assertEqual(sorted(os.listdir(self.directory.name)), ["frozen.db", "pluggy.db"])

    def test_unknown_file_is_refused(self) -> None:
        other = os.path.join(self.directory.name, "other.db")
        db = sqlite3.connect(other)
        db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
        db.commit()
        db.close()

        self.assertEqual(classify(other), "unknown")
        with self.assertRaises(UnknownDatabase):
            open_ledger(other)


if __name__ == "__main__":
    unittest.main()
