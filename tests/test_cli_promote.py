"""The command line around the promotion: the default sync is the ledger and
refuses a file that has not been cut over; `--promote` runs the six steps."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pluggysync.cli import main
from pluggysync.ledger import classify, open_ledger
from pluggysync.legacy_import import import_states
from pluggysync.legacy_schema import LEGACY_SCHEMA
from pluggysync.run import LedgerRun
from pluggysync.walk import Client
from tests.fakes import FakeClock, FakeTransport
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, script_two_items
from tests.test_legacy_import import build_state


class PromoteCliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = self.directory.name
        self.sep, self.nov = os.path.join(root, "sep.db"), os.path.join(root, "nov.db")
        build_state(self.sep, "legacy_sep.sql")
        build_state(self.nov, "legacy_nov.sql")
        self.live = os.path.join(root, "pluggy.db")
        shutil.copyfile(self.nov, self.live)
        self.candidate = os.path.join(root, "candidate.db")
        self.clock = FakeClock()
        self.clock.t = 400 * 24 * 3600
        self.transport = FakeTransport(self.clock)
        script_two_items(self.transport)
        db = open_ledger(self.candidate)
        import_states(db, [self.sep, self.nov])
        LedgerRun(db, [ITEM_A, ITEM_B], self.make_client, self.clock, "test").execute()
        db.close()
        # A fresh transport for the command under test: the one above is spent.
        self.transport = FakeTransport(self.clock)
        script_two_items(self.transport)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env.update(
            PLUGGY_DB=self.live,
            PLUGGY_CLIENT_ID="placeholder",
            PLUGGY_CLIENT_SECRET="placeholder",
            PLUGGY_ITEM_IDS=f"{ITEM_A},{ITEM_B}",
        )
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def make_client(self, on_attempt) -> Client:
        return Client("placeholder", "placeholder", on_attempt, self.transport, self.clock)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv), make_client=self.make_client, clock=self.clock)
        return code, out.getvalue(), err.getvalue()


class DefaultSyncTest(PromoteCliCase):
    def test_the_default_refuses_the_legacy_file(self) -> None:
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("never migrated in place", err)
        self.assertEqual(self.transport.requests, [])

    def test_the_default_refuses_a_ledger_without_a_cutover_row(self) -> None:
        os.unlink(self.live)
        shutil.copyfile(self.candidate, self.live)

        code, _, err = self.run_cli()

        self.assertEqual(code, 1)
        self.assertIn("--candidate", err)
        self.assertIn("--promote", err)
        self.assertEqual(self.transport.requests, [])

    def test_missing_configuration_is_exit_2_before_anything_is_opened(self) -> None:
        del os.environ["PLUGGY_ITEM_IDS"]
        os.unlink(self.live)

        code, _, err = self.run_cli()

        self.assertEqual(code, 2)
        self.assertIn("PLUGGY_ITEM_IDS", err)
        self.assertFalse(os.path.exists(self.live))


class PromoteModeTest(PromoteCliCase):
    def test_promote_then_the_default_sync_runs_the_ledger_on_the_promoted_file(self) -> None:
        code, out, err = self.run_cli(
            "--promote", self.candidate, "--from-states", self.sep, self.nov
        )

        self.assertEqual(code, 0, err)
        self.assertIn("6 promoted file checked", out)
        self.assertEqual(classify(self.live), "ledger")
        self.assertTrue(os.path.exists(self.live + "-legacy-final"))

        # A fresh transport for the next run: the previous one's queues are spent.
        self.transport = FakeTransport(self.clock)
        script_two_items(self.transport)
        code, out, err = self.run_cli()

        self.assertEqual(code, 0, err)
        self.assertIn("run 5: complete", out)
        db = sqlite3.connect(f"file:{self.live}?mode=ro", uri=True)
        self.addCleanup(db.close)
        self.assertEqual(
            db.execute("SELECT id, outcome FROM runs ORDER BY id").fetchall(),
            [(1, "migrated"), (2, "migrated"), (3, "migrated"), (4, "complete"), (5, "complete")],
        )
        self.assertEqual(db.execute("SELECT count(*) FROM cutover").fetchone()[0], 1)

    def test_promote_without_from_states_is_refused(self) -> None:
        code, _, err = self.run_cli("--promote", self.candidate)
        self.assertEqual(code, 2)
        self.assertIn("--from-states", err)
        self.assertEqual(classify(self.live), "legacy")

    def test_promote_of_the_live_file_onto_itself_is_refused(self) -> None:
        code, _, err = self.run_cli("--promote", self.live, "--from-states", self.sep, self.nov)
        self.assertEqual(code, 2)
        self.assertIn("live database", err)

    def test_promote_refuses_a_live_file_that_diverged(self) -> None:
        legacy = sqlite3.connect(self.live)
        legacy.execute("UPDATE probes SET rows = 99 WHERE rowid = 1")
        legacy.commit()
        legacy.close()

        code, _, err = self.run_cli(
            "--promote", self.candidate, "--from-states", self.sep, self.nov
        )

        self.assertEqual(code, 1)
        self.assertIn("diverges", err)
        self.assertEqual(classify(self.live), "legacy")
        self.assertIn("CREATE TABLE IF NOT EXISTS probes", LEGACY_SCHEMA)


if __name__ == "__main__":
    unittest.main()
