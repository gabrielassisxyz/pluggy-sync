"""Cases the review of the integrity check added: a swept scope is healthy, a
stored day is never recomputed, an ambiguous run is not a move, and the
command line refuses what it cannot do instead of falling through."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pluggysync.check import _moved_problems, check_file
from pluggysync.cli import main
from pluggysync.ledger import SCHEMA_V1
from pluggysync.reparse import ReparseRefused, reparse
from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.ledger_run import ACCOUNT_A1, ITEM_A, ITEM_B, LedgerHarness, script_two_items
from tests.fixtures.pages import cursor_query


class Crash(BaseException):
    pass


def crash_on(url_fragment: str):
    def hook(name: str, **context: object) -> None:
        if name == "after_archive_commit" and url_fragment in str(context.get("url")):
            raise Crash(name)

    return hook


class SweptScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        transport = self.harness.transport()
        script_two_items(transport)
        with self.assertRaises(Crash):
            self.harness.run(
                transport, [ITEM_A, ITEM_B], crash_at=crash_on(cursor_query(ACCOUNT_A1))
            ).execute()
        self.harness.reopen()
        self.harness.run(self.harness.transport(), [ITEM_A, ITEM_B]).sweep()
        self.harness.connection.close()

    def test_a_database_with_a_swept_run_passes_the_check(self) -> None:
        code, problems, _ = check_file(self.harness.path)
        self.assertEqual((code, problems), (0, []))

    def test_a_run_still_running_refuses_the_reparse(self) -> None:
        db = sqlite3.connect(self.harness.path, isolation_level=None)
        db.execute(
            "INSERT INTO runs (started_at, started_day, outcome, code_version, item_ids)"
            " VALUES ('2026-01-09T12:00:00+00:00', '2026-01-09', 'running', 'test', '[]')"
        )
        db.close()
        destination = os.path.join(os.path.dirname(self.harness.path), "reparsed.db")

        with self.assertRaises(ReparseRefused) as refused:
            reparse(self.harness.path, destination)

        self.assertIn("still running", str(refused.exception))
        self.assertFalse(os.path.exists(destination))


class StoredDayTest(unittest.TestCase):
    def test_a_stored_closed_day_is_not_recomputed_under_today_s_rules(self) -> None:
        harness = LedgerHarness()
        self.addCleanup(harness.close)
        transport = harness.transport()
        script_two_items(transport)
        harness.run(transport, [ITEM_A, ITEM_B]).execute()
        db = harness.connection
        trigger = next(s for s in SCHEMA_V1 if "CREATE TRIGGER scopes_close_once" in s)
        db.execute("DROP TRIGGER scopes_close_once")
        db.execute("UPDATE scopes SET closed_day = '1999-12-31'")
        db.execute(trigger)
        db.close()

        code, problems, _ = check_file(harness.path)

        self.assertEqual((code, problems), (0, []))


class AmbiguousMoveTest(unittest.TestCase):
    def test_two_bodies_under_a_new_parent_is_not_a_missing_move(self) -> None:
        ledger = HandBuilt()
        self.addCleanup(ledger.close)
        run1 = ledger.run()
        ledger.sight(
            run1,
            ledger.scope(run1, "transactions", "acc-a", min_date="2026-01-01"),
            {"id": "t1", "amount": 1.0},
        )
        ledger.project(run1)
        run2 = ledger.run()
        scope = ledger.scope(run2, "transactions", "acc-b", min_date="2026-01-01")
        ledger.sight(run2, scope, {"id": "t1", "amount": 1.0})
        ledger.sight(run2, scope, {"id": "t1", "amount": 2.0})
        ledger.project(run2)

        info: list[str] = []
        self.assertEqual(_moved_problems(ledger.db, info), [])


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env["PLUGGY_DB"] = os.path.join(self.directory.name, "missing.db")
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(list(argv))
        return code, err.getvalue()

    def test_retime_alone_is_refused_instead_of_running_the_legacy_sync(self) -> None:
        code, err = self.run_cli("--retime")
        self.assertEqual(code, 2)
        self.assertIn("--retime needs --reparse", err)
        self.assertFalse(os.path.exists(os.environ["PLUGGY_DB"]))

    def test_check_of_a_missing_file_is_a_message_not_a_traceback(self) -> None:
        code, err = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertFalse(os.path.exists(os.environ["PLUGGY_DB"] + ".lock"))


if __name__ == "__main__":
    unittest.main()
