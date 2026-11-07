"""`--candidate` keeps the ledger off the live file and out of version control."""

from __future__ import annotations

import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pluggysync.cli import main
from pluggysync.legacy_schema import LEGACY_SCHEMA
from pluggysync.walk import Client
from tests.fakes import FakeClock, FakeTransport
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, script_two_items

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CandidateCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.live = os.path.join(self.directory.name, "live.db")
        sqlite3.connect(self.live).close()
        self.clock = FakeClock()
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

    def test_the_live_database_is_refused(self) -> None:
        code, _, err = self.run_cli("--candidate", self.live)
        self.assertEqual(code, 2)
        self.assertIn("is the live database", err)
        self.assertEqual(self.transport.requests, [])

    def test_the_live_path_is_refused_even_before_the_file_exists(self) -> None:
        os.unlink(self.live)

        code, _, err = self.run_cli("--candidate", self.live)

        self.assertEqual(code, 2)
        self.assertIn("is the live database", err)
        self.assertFalse(os.path.exists(self.live))

    def test_a_relative_alias_of_the_live_database_is_refused(self) -> None:
        previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, previous)

        code, _, err = self.run_cli("--candidate", "./live.db")

        self.assertEqual(code, 2)
        self.assertIn("live database", err)

    def test_an_unignored_path_inside_the_repository_is_refused_without_writing(self) -> None:
        inside = ROOT / "candidate-under-test.db"
        self.addCleanup(lambda: inside.unlink(missing_ok=True))
        self.assertFalse(inside.exists())

        code, _, err = self.run_cli("--candidate", str(inside))

        self.assertEqual(code, 2)
        self.assertIn("does not ignore it", err)
        self.assertFalse(inside.exists())

    def test_an_ignored_path_gets_a_private_candidate_and_a_ledger_run(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")

        code, out, err = self.run_cli("--candidate", candidate)

        self.assertEqual(code, 0, err)
        self.assertEqual(os.stat(candidate).st_mode & 0o777, 0o600)
        self.assertIn("run 1: complete", out)
        db = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT outcome FROM runs").fetchall(), [("complete",)])
        self.assertEqual(db.execute("SELECT count(*) FROM transactions").fetchone()[0], 4)
        self.assertEqual(os.path.getsize(self.live), 0)

    def test_rebuild_against_a_candidate(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")
        self.run_cli("--candidate", candidate)

        code, out, _ = self.run_cli("--rebuild", "--candidate", candidate)

        self.assertEqual(code, 0)
        self.assertIn("rebuilt from 1 runs", out)

    def test_rebuild_against_the_legacy_live_database_is_refused(self) -> None:
        legacy = sqlite3.connect(self.live)
        legacy.executescript(LEGACY_SCHEMA)
        legacy.execute("INSERT INTO sync_runs (started_at) VALUES ('x')")
        legacy.commit()
        legacy.close()

        code, _, err = self.run_cli("--rebuild")

        self.assertEqual(code, 1)
        self.assertIn("never migrated in place", err)

    def test_missing_configuration_is_exit_2(self) -> None:
        del os.environ["PLUGGY_ITEM_IDS"]
        candidate = os.path.join(self.directory.name, "candidate.db")

        code, _, err = self.run_cli("--candidate", candidate)

        self.assertEqual(code, 2)
        self.assertIn("PLUGGY_ITEM_IDS", err)
        self.assertFalse(os.path.exists(candidate))


if __name__ == "__main__":
    unittest.main()
