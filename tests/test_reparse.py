"""`--reparse` on temporary databases: equality, wrong mapping, races, retime, cutover."""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import pluggysync.derive as derive_module
from pluggysync.check import check_file
from pluggysync.cli import main
from pluggysync.ledger import local_day
from pluggysync.reparse import ReparseRefused, reparse
from pluggysync.walk import Page
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ITEM_A,
    ITEM_B,
    LedgerHarness,
    script_two_items,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLES = (
    "runs",
    "scopes",
    "responses",
    "documents",
    "versions",
    "observations",
    "page_derivations",
    "events",
    "ambiguities",
    "document_state",
    "projection_state",
    "items",
    "accounts",
    "transactions",
    "bills",
    "investments",
    "investment_transactions",
    "consents",
    "identities",
    "categories",
    "legacy_sync_runs",
    "legacy_probes",
    "cutover",
)


def _sha(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return sorted(connection.execute(f"SELECT * FROM {table}").fetchall())


class ReparseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)

    def two_runs(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.harness.run(first, [ITEM_A, ITEM_B]).execute()
        second = self.harness.transport()
        script_two_items(second)
        self.harness.run(second, [ITEM_A, ITEM_B]).execute()

    def dest(self) -> str:
        return os.path.join(self.harness.directory.name, "reparsed.db")


class EqualityTest(ReparseCase):
    def test_same_parser_produces_equal_layers_and_passes_check(self) -> None:
        self.two_runs()
        source = self.harness.path
        before = _sha(source)
        destination = self.dest()

        reparse(source, destination)

        self.assertEqual(_sha(source), before)
        self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
        code, problems, _ = check_file(destination)
        self.assertEqual(code, 0, problems)
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            for table in TABLES:
                self.assertEqual(_rows(dst, table), _rows(src, table), table)
        finally:
            src.close()
            dst.close()

    def test_cli_reparse_exits_0(self) -> None:
        self.two_runs()
        destination = self.dest()
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env.update(
            {
                "PLUGGY_DB": self.harness.path,
                "PLUGGY_CLIENT_ID": "placeholder",
                "PLUGGY_CLIENT_SECRET": "placeholder",
                "PLUGGY_ITEM_IDS": f"{ITEM_A},{ITEM_B}",
            }
        )
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--reparse", destination])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(os.path.exists(destination))


def build_wrong_fixture(harness: LedgerHarness) -> int:
    """A source built with a parser that misreads one account away."""
    original = derive_module.derive_page

    def wrong(
        connection: sqlite3.Connection, scope, page, received_at: str, received_day: str
    ) -> bool:
        if scope.kind == "accounts":
            kept = [d for d in page.documents if d.get("id") != ACCOUNT_A1]
            page = Page(page.attempt_id, kept, page.error, page.page, page.total_pages, page.next)
        return original(connection, scope, page, received_at, received_day)

    derive_module.derive_page = wrong  # type: ignore[method-assign]
    try:
        transport = harness.transport()
        script_two_items(transport)
        run_id = harness.run(transport, [ITEM_A, ITEM_B]).execute()
    finally:
        derive_module.derive_page = original  # type: ignore[method-assign]
    connection = harness.connection
    connection.execute(
        "INSERT INTO legacy_sync_runs (id, started_at) VALUES (1, '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO legacy_probes (seq, run_id, path, status, rows, fetched_at)"
        " VALUES (1, 1, '/categories', 200, 1, '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO runs (started_at, started_day, finished_at, finished_day, outcome,"
        " code_version, item_ids) VALUES (?,?,?,?,?,?,?)",
        (
            "2025-01-01T12:00:00+00:00",
            "2025-01-01",
            "2025-01-01T12:00:00+00:00",
            "2025-01-01",
            "migrated",
            "test",
            "[]",
        ),
    )
    migrated_run = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO scopes (run_id, kind, parent, expected, outcome, closed_at,"
        " closed_day, rows) VALUES (?,?,?,?,?,?,?,?)",
        (
            migrated_run,
            "accounts",
            ITEM_A,
            1,
            "complete",
            "2025-01-01T12:00:00+00:00",
            "2025-01-01",
            1,
        ),
    )
    migrated_scope = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO documents (kind, provider_id) VALUES (?,?)", ("accounts", "acc-old")
    )
    document_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    from pluggysync.derive import canonical as _canonical

    sha, body = _canonical({"id": "acc-old", "itemId": "x"})
    connection.execute(
        "INSERT INTO versions (sha256, body) VALUES (?,?) ON CONFLICT DO NOTHING",
        (sha, body),
    )
    connection.execute(
        "INSERT INTO observations (run_id, scope_id, response_id, position, document_id,"
        " parent, sha256, observed_at, observed_day) VALUES (?,?,NULL,NULL,?,?,?,?,?)",
        (
            migrated_run,
            migrated_scope,
            document_id,
            ITEM_A,
            sha,
            "2025-01-01T12:00:00+00:00",
            "2025-01-01",
        ),
    )
    connection.commit()
    return run_id


class WrongMappingTest(ReparseCase):
    def _build_wrong(self) -> int:
        return build_wrong_fixture(self.harness)

    def test_corrected_parser_restores_scope_children_events_and_outcome(self) -> None:
        run_id = self._build_wrong()
        source = self.harness.path
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            old_responses = _rows(src, "responses")
            old_migrated = src.execute(
                "SELECT * FROM observations WHERE run_id != ? ORDER BY id", (run_id,)
            ).fetchall()
            old_legacy_runs = _rows(src, "legacy_sync_runs")
            old_legacy_probes = _rows(src, "legacy_probes")
            old_cutover = _rows(src, "cutover")
        finally:
            src.close()
        destination = self.dest()

        reparse(source, destination)

        code, problems, _ = check_file(destination)
        self.assertEqual(code, 0, problems)
        dst = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            self.assertEqual(_rows(dst, "responses"), old_responses)
            new_migrated = dst.execute(
                "SELECT run_id, scope_id, response_id, position, document_id,"
                " parent, sha256, observed_at, observed_day FROM observations"
                " WHERE run_id != ? ORDER BY run_id, scope_id, document_id",
                (run_id,),
            ).fetchall()
            old_stripped = [tuple(r[1:10]) for r in old_migrated]
            self.assertEqual(sorted(new_migrated), sorted(old_stripped))
            self.assertEqual(_rows(dst, "legacy_sync_runs"), old_legacy_runs)
            self.assertEqual(_rows(dst, "legacy_probes"), old_legacy_probes)
            self.assertEqual(_rows(dst, "cutover"), old_cutover)
            corrected = dst.execute(
                "SELECT outcome, rows FROM scopes WHERE run_id = ? AND kind = 'accounts'"
                " AND parent = ?",
                (run_id, ITEM_A),
            ).fetchone()
            self.assertEqual(tuple(corrected), ("complete", 2))
            child = dst.execute(
                "SELECT outcome, expected, rows FROM scopes WHERE run_id = ?"
                " AND kind = 'transactions' AND parent = ?",
                (run_id, ACCOUNT_A1),
            ).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual(tuple(child), ("skipped", 1, 0))
            outcome = dst.execute("SELECT outcome FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
            self.assertEqual(outcome, "partial")
            self.assertGreater(
                dst.execute("SELECT count(*) FROM document_state").fetchone()[0],
                0,
            )
        finally:
            dst.close()


class RefusalTest(ReparseCase):
    def test_existing_path_leaves_no_file_behind(self) -> None:
        self.two_runs()
        destination = self.dest()
        with open(destination, "w") as handle:
            handle.write("existing")
        with self.assertRaises(ReparseRefused):
            reparse(self.harness.path, destination)
        with open(destination) as handle:
            self.assertEqual(handle.read(), "existing")

    def test_unignored_path_inside_repository_is_refused(self) -> None:
        self.two_runs()
        inside = ROOT / "candidate-under-test.db"
        self.addCleanup(lambda: inside.unlink(missing_ok=True))
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env.update(
            {
                "PLUGGY_DB": self.harness.path,
                "PLUGGY_CLIENT_ID": "placeholder",
                "PLUGGY_CLIENT_SECRET": "placeholder",
                "PLUGGY_ITEM_IDS": f"{ITEM_A},{ITEM_B}",
            }
        )
        with mock.patch.dict(os.environ, env, clear=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--reparse", str(inside)])
        self.assertEqual(code, 2)
        self.assertIn("does not ignore it", err.getvalue())
        self.assertFalse(inside.exists())

    def test_failure_mid_build_leaves_no_file(self) -> None:
        self.two_runs()
        destination = self.dest()

        def fail(name: str, **context: object) -> None:
            if name == "before_rebuild":
                raise RuntimeError("injected failure")

        with self.assertRaises(RuntimeError):
            reparse(self.harness.path, destination, crash_at=fail)
        self.assertFalse(os.path.lexists(destination))

    def test_destination_created_before_publish_is_left_untouched(self) -> None:
        self.two_runs()
        destination = self.dest()

        def race() -> None:
            with open(destination, "w") as handle:
                handle.write("racing")

        with self.assertRaises(RuntimeError):
            reparse(self.harness.path, destination, before_publish=race)
        with open(destination) as handle:
            self.assertEqual(handle.read(), "racing")


class RetimeTest(ReparseCase):
    def test_observed_day_preserved_without_retime_and_recomputed_with_it(self) -> None:
        self.two_runs()
        connection = self.harness.connection
        row = connection.execute(
            "SELECT id, received_at FROM responses WHERE body_zlib IS NOT NULL LIMIT 1"
        ).fetchone()
        response_id, received_at = row
        right_day = local_day(str(received_at))
        wrong_day = "2000-01-02" if right_day != "2000-01-02" else "2000-01-03"
        import sqlite3 as _sqlite

        raw = _sqlite.connect(self.harness.path)
        try:
            triggers = raw.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            for name, _ in triggers:
                raw.execute(f"DROP TRIGGER {name}")
            raw.execute(
                "UPDATE responses SET received_day = ? WHERE id = ?", (wrong_day, response_id)
            )
            raw.execute(
                "UPDATE observations SET observed_day = ? WHERE response_id = ?",
                (wrong_day, response_id),
            )
            raw.commit()
            for _, sql in triggers:
                raw.execute(sql)
            raw.commit()
        finally:
            raw.close()
        plain = os.path.join(self.harness.directory.name, "plain.db")
        retimed = os.path.join(self.harness.directory.name, "retimed.db")
        reparse(self.harness.path, plain)
        reparse(self.harness.path, retimed, retime=True)
        check_plain, _, _ = check_file(plain)
        check_retimed, _, _ = check_file(retimed)
        self.assertEqual(check_plain, 0)
        self.assertEqual(check_retimed, 0)
        for path, want in ((plain, wrong_day), (retimed, right_day)):
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                got = connection.execute(
                    "SELECT observed_day FROM observations WHERE response_id = ? LIMIT 1",
                    (response_id,),
                ).fetchone()[0]
                self.assertEqual(got, want)
                state = connection.execute(
                    "SELECT code_version FROM projection_state WHERE id = 1"
                ).fetchone()[0]
                if path == retimed:
                    self.assertIn("retime", state)
            finally:
                connection.close()


class CutoverTest(ReparseCase):
    def test_acceptance_run_becoming_partial_refuses_publication(self) -> None:
        run_id = build_wrong_fixture(self.harness)
        self.harness.connection.execute(
            "INSERT INTO cutover (id, completed_at, from_states, acceptance_run)"
            " VALUES (1, '2026-01-02T00:00:00+00:00', '[]', ?)",
            (run_id,),
        )
        self.harness.connection.commit()
        destination = self.dest()
        with self.assertRaises(RuntimeError) as raised:
            reparse(self.harness.path, destination)
        self.assertIn("new live acceptance run is needed first", str(raised.exception))
        self.assertFalse(os.path.lexists(destination))


if __name__ == "__main__":
    unittest.main()
