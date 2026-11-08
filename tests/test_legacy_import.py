"""The frozen legacy states become the first runs of a candidate ledger, and the
import refuses anything it cannot account for."""

from __future__ import annotations

import io
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pluggysync.cli import main
from pluggysync.coverage import projected_run
from pluggysync.ledger import open_ledger
from pluggysync.legacy_import import ImportFailed, import_states
from pluggysync.legacy_schema import LEGACY_SCHEMA

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def build_state(path: str, script: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(LEGACY_SCHEMA)
    db.executescript((FIXTURES / script).read_text())
    db.commit()
    db.close()


def edit_state(path: str, sql: str) -> None:
    db = sqlite3.connect(path)
    db.execute(sql)
    db.commit()
    db.close()


class ImportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sep = os.path.join(self.directory.name, "sep.db")
        self.nov = os.path.join(self.directory.name, "nov.db")
        build_state(self.sep, "legacy_sep.sql")
        build_state(self.nov, "legacy_nov.sql")
        self.candidate = os.path.join(self.directory.name, "candidate.db")

    def run_import(self) -> sqlite3.Connection:
        db = open_ledger(self.candidate)
        self.addCleanup(db.close)
        import_states(db, [self.sep, self.nov])
        return db


class HappyImportTest(ImportCase):
    def test_runs_scopes_observations_and_legacy_tables(self) -> None:
        db = self.run_import()

        self.assertEqual(
            db.execute(
                "SELECT id, outcome, code_version, api_calls, item_ids FROM runs ORDER BY id"
            ).fetchall(),
            [
                (1, "migrated", "unknown", 7, '["item-0001"]'),
                (2, "migrated", "unknown", 7, '["item-0001"]'),
                (3, "migrated", "unknown", 7, '["item-0001"]'),
            ],
        )
        self.assertIn(
            "no row survived", db.execute("SELECT note FROM runs WHERE id = 1").fetchone()[0]
        )
        self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 20)
        self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 11)
        scopes = db.execute(
            "SELECT run_id, kind, parent, outcome, infer_absence, status, rows, error FROM scopes"
            " ORDER BY run_id, kind, parent"
        ).fetchall()
        self.assertEqual(len(scopes), 23)
        self.assertTrue(
            all(
                s[3] == "incomplete" and s[4] == 0 and s[7] == "legacy: completeness unknown"
                for s in scopes
            )
        )
        synthetic = {(s[0], s[1]) for s in scopes if s[5] is None}
        self.assertEqual(
            synthetic,
            {
                (1, "consents"),
                (2, "consents"),
                (3, "consents"),
                (2, "investment_transactions"),
                (3, "investment_transactions"),
            },
        )
        self.assertEqual(
            db.execute(
                "SELECT rows FROM scopes WHERE run_id = 2 AND kind = 'transactions'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(db.execute("SELECT count(*) FROM legacy_probes").fetchone()[0], 18)
        self.assertEqual(db.execute("SELECT count(*) FROM legacy_sync_runs").fetchone()[0], 3)
        self.assertEqual(
            db.execute(
                "SELECT count(*) FROM observations WHERE response_id IS NULL AND position IS NULL"
            ).fetchone()[0],
            20,
        )

    def test_the_repeated_pending_row_is_one_sighting_in_run_2(self) -> None:
        db = self.run_import()

        rows = db.execute(
            "SELECT o.run_id FROM observations o JOIN documents d ON d.id = o.document_id"
            " WHERE d.provider_id = 'tx-pending'"
        ).fetchall()
        self.assertEqual(rows, [(2,)])
        self.assertEqual(
            db.execute(
                "SELECT first_seen_run, last_seen_run, state FROM transactions"
                " WHERE id = 'tx-pending'"
            ).fetchone(),
            (2, 2, "present"),
        )
        self.assertEqual(
            db.execute(
                "SELECT first_seen_run, last_seen_run FROM transactions WHERE id = 'tx-0001'"
            ).fetchone(),
            (2, 3),
        )
        self.assertEqual(
            db.execute(
                "SELECT first_seen_run, last_seen_run FROM transactions WHERE id = 'tx-0002'"
            ).fetchone(),
            (3, 3),
        )

    def test_parents_bodies_and_days_come_from_the_rows(self) -> None:
        db = self.run_import()

        self.assertEqual(
            db.execute(
                "SELECT DISTINCT parent FROM observations o"
                " JOIN documents d ON d.id = o.document_id WHERE d.kind = 'categories'"
            ).fetchall(),
            [("",)],
        )
        self.assertEqual(
            db.execute(
                "SELECT parent FROM observations o"
                " JOIN documents d ON d.id = o.document_id WHERE d.kind = 'items'"
            ).fetchall(),
            [("item-0001",), ("item-0001",)],
        )
        self.assertEqual(
            db.execute(
                "SELECT DISTINCT parent FROM observations o"
                " JOIN documents d ON d.id = o.document_id"
                " WHERE d.kind = 'investment_transactions'"
            ).fetchall(),
            [("investment-0001",)],
        )
        body = db.execute("SELECT raw FROM transactions WHERE id = 'tx-0001'").fetchone()[0]
        self.assertEqual(json.loads(body)["date"], "2026-09-10T15:00:00.000Z")
        self.assertTrue(body.startswith('{"amount":'))
        self.assertEqual(
            db.execute(
                "SELECT DISTINCT observed_day FROM observations WHERE run_id = 3"
            ).fetchall(),
            [("2026-11-07",)],
        )
        self.assertEqual(projected_run(db), 3)
        # The item, the account, the investment and one transaction changed between runs.
        self.assertEqual(
            db.execute(
                "SELECT event, count(*) FROM events GROUP BY event ORDER BY event"
            ).fetchall(),
            [("appeared", 11), ("changed", 4)],
        )
        self.assertEqual(
            db.execute("SELECT outcome FROM run_coverage ORDER BY run_id").fetchall(),
            [("migrated",)] * 3,
        )


class RefusalTest(ImportCase):
    def assert_refused(self, fragment: str, states: list[str] | None = None) -> None:
        db = open_ledger(self.candidate)
        self.addCleanup(db.close)
        with self.assertRaises(ImportFailed) as refused:
            import_states(db, states if states is not None else [self.sep, self.nov])
        self.assertIn(fragment, str(refused.exception))
        self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 0)

    def test_a_row_matching_no_window(self) -> None:
        edit_state(
            self.nov,
            "UPDATE transactions SET fetched_at = '2026-10-01T00:00:00+00:00' WHERE id = 'tx-0002'",
        )
        self.assert_refused("matches 0 run windows")

    def test_a_row_matching_two_windows(self) -> None:
        edit_state(
            self.nov, "UPDATE sync_runs SET finished_at = '2026-09-12T18:52:40+00:00' WHERE id = 1"
        )
        edit_state(
            self.sep, "UPDATE sync_runs SET finished_at = '2026-09-12T18:52:40+00:00' WHERE id = 1"
        )
        self.assert_refused("matches 2 run windows")

    def test_a_row_repeated_in_one_run_with_another_body(self) -> None:
        edit_state(
            self.nov,
            "UPDATE transactions SET raw = replace(raw, '-50.36', '-50.37')"
            " WHERE id = 'tx-pending'",
        )
        self.assert_refused("differs between states inside run 2")

    def test_states_that_are_not_a_prefix(self) -> None:
        edit_state(self.sep, "UPDATE probes SET status = 500 WHERE rowid = 1")
        self.assert_refused("not a prefix")

    def test_no_states_at_all(self) -> None:
        self.assert_refused("at least one state", states=[])

    def test_a_candidate_that_already_holds_runs(self) -> None:
        db = open_ledger(self.candidate)
        import_states(db, [self.sep, self.nov])
        with self.assertRaises(ImportFailed) as refused:
            import_states(db, [self.sep, self.nov])
        db.close()
        self.assertIn("already holds runs", str(refused.exception))


class ImportCliTest(ImportCase):
    def test_the_live_database_is_never_opened(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env["PLUGGY_DB"] = os.path.join(self.directory.name, "does-not-exist.db")
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(["--candidate", self.candidate, "--from-states", self.sep, self.nov])

        self.assertEqual(code, 0, err.getvalue())
        self.assertFalse(os.path.exists(env["PLUGGY_DB"]))
        self.assertIn("20 sightings", out.getvalue())
        self.assertEqual(os.stat(self.candidate).st_mode & 0o777, 0o600)

    def test_from_states_without_candidate_is_refused(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--from-states", self.sep, self.nov])
        self.assertEqual(code, 2)
        self.assertIn("--candidate", err.getvalue())

    def test_a_failed_import_leaves_no_candidate_behind(self) -> None:
        edit_state(
            self.nov,
            "UPDATE transactions SET fetched_at = '2026-10-01T00:00:00+00:00' WHERE id = 'tx-0002'",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env["PLUGGY_DB"] = os.path.join(self.directory.name, "live.db")
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err),
        ):
            code = main(["--candidate", self.candidate, "--from-states", self.sep, self.nov])

        self.assertEqual(code, 1)
        self.assertIn("matches 0 run windows", err.getvalue())
        self.assertFalse(os.path.exists(self.candidate))


if __name__ == "__main__":
    unittest.main()
