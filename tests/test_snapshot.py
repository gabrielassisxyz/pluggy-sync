"""A consistent snapshot after every run, for the backup job to pick up."""

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
from pluggysync.snapshot import SnapshotError, snapshot_path_for, write_snapshot
from pluggysync.walk import Client
from tests.fakes import FakeClock, FakeTransport, ok
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, script_two_items
from tests.fixtures.pages import paged_page

ROOT = pathlib.Path(__file__).resolve().parents[1]


def table_counts(path: str) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            name: int(connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
            for name in tables
        }
    finally:
        connection.close()


class SnapshotCliTest(unittest.TestCase):
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

    def test_success_writes_a_verified_snapshot_with_the_same_row_counts(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")

        code, out, err = self.run_cli("--candidate", candidate)

        self.assertEqual(code, 0, err)
        snapshot = os.path.join(self.directory.name, "pluggy-snapshot.db")
        self.assertTrue(os.path.exists(snapshot), out)
        self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)
        verdict = (
            sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            .execute("PRAGMA integrity_check")
            .fetchone()[0]
        )
        self.assertEqual(verdict, "ok")
        self.assertEqual(table_counts(snapshot), table_counts(candidate))

    def test_custom_path_is_honoured(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")
        custom = os.path.join(self.directory.name, "custom-snap.db")
        os.environ["PLUGGY_SNAPSHOT"] = custom

        code, _, err = self.run_cli("--candidate", candidate)

        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(custom))
        self.assertEqual(os.stat(custom).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.directory.name, "pluggy-snapshot.db")))

    def test_gitignore_covers_the_default_name(self) -> None:
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("pluggy-snapshot.db", ignored)

    def test_snapshot_failure_leaves_the_previous_file_and_exits_1(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")
        code, _, err = self.run_cli("--candidate", candidate)
        self.assertEqual(code, 0, err)
        snapshot = os.path.join(self.directory.name, "pluggy-snapshot.db")
        with open(snapshot, "rb") as handle:
            before = handle.read()
        with mock.patch(
            "pluggysync.snapshot._verified_copy", side_effect=OSError("unwritable directory")
        ):
            failing = FakeTransport(self.clock)
            script_two_items(failing)
            self.transport = failing
            code, _, err = self.run_cli("--candidate", candidate)
        self.assertEqual(code, 1)
        self.assertIn("snapshot", err)
        with open(snapshot, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_failed_run_writes_the_snapshot_when_the_self_check_passes(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")
        sparse = FakeTransport(self.clock)
        sparse.answer(
            "/categories?pageSize=500&page=1",
            ok(paged_page([{"id": "01000000", "description": "Income", "parentId": None}])),
        )
        self.transport = sparse

        code, out, _ = self.run_cli("--candidate", candidate)

        self.assertEqual(code, 1)
        self.assertIn("failed", out)
        snapshot = os.path.join(self.directory.name, "pluggy-snapshot.db")
        self.assertTrue(os.path.exists(snapshot))
        self.assertEqual(table_counts(snapshot), table_counts(candidate))

    def test_failed_run_keeps_the_previous_snapshot_when_the_self_check_fails(self) -> None:
        candidate = os.path.join(self.directory.name, "candidate.db")
        code, _, err = self.run_cli("--candidate", candidate)
        self.assertEqual(code, 0, err)
        snapshot = os.path.join(self.directory.name, "pluggy-snapshot.db")
        with open(snapshot, "rb") as handle:
            before = handle.read()
        sparse = FakeTransport(self.clock)
        sparse.answer(
            "/categories?pageSize=500&page=1",
            ok(paged_page([{"id": "01000000", "description": "Income", "parentId": None}])),
        )
        self.transport = sparse
        with mock.patch("pluggysync.cli.self_check", return_value=["injected self-check problem"]):
            code, _, err = self.run_cli("--candidate", candidate)
        self.assertEqual(code, 1)
        with open(snapshot, "rb") as handle:
            self.assertEqual(handle.read(), before)


class SnapshotModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.live = os.path.join(self.directory.name, "live.db")
        sqlite3.connect(self.live).close()
        self.clock = FakeClock()
        self.transport = FakeTransport(self.clock)
        script_two_items(self.transport)
        self.candidate = os.path.join(self.directory.name, "candidate.db")
        self.snapshot = os.path.join(self.directory.name, "pluggy-snapshot.db")

    def run_fixture(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env.update(
            PLUGGY_DB=self.live,
            PLUGGY_CLIENT_ID="placeholder",
            PLUGGY_CLIENT_SECRET="placeholder",
            PLUGGY_ITEM_IDS=f"{ITEM_A},{ITEM_B}",
        )
        with mock.patch.dict(os.environ, env, clear=True):
            out, err = io.StringIO(), io.StringIO()

            def make_client(on_attempt) -> Client:
                return Client("placeholder", "placeholder", on_attempt, self.transport, self.clock)

            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(
                    main(
                        ["--candidate", self.candidate],
                        make_client=make_client,
                        clock=self.clock,
                    ),
                    0,
                    err.getvalue(),
                )

    def test_write_snapshot_copies_every_table_at_mode_0600(self) -> None:
        self.run_fixture()

        returned = write_snapshot(self.candidate, self.snapshot)

        self.assertEqual(returned, self.snapshot)
        self.assertEqual(os.stat(self.snapshot).st_mode & 0o777, 0o600)
        verdict = (
            sqlite3.connect(f"file:{self.snapshot}?mode=ro", uri=True)
            .execute("PRAGMA integrity_check")
            .fetchone()[0]
        )
        self.assertEqual(verdict, "ok")
        self.assertEqual(table_counts(self.snapshot), table_counts(self.candidate))

    def test_write_snapshot_refuses_a_missing_directory(self) -> None:
        self.run_fixture()
        missing = os.path.join(self.directory.name, "absent", "snap.db")

        with self.assertRaises(SnapshotError):
            write_snapshot(self.candidate, missing)

        self.assertFalse(os.path.exists(missing))

    def test_snapshot_path_for_defaults_beside_the_database(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("PLUGGY_SNAPSHOT", None)
            self.assertEqual(
                snapshot_path_for(self.candidate),
                os.path.join(self.directory.name, "pluggy-snapshot.db"),
            )

    def test_snapshot_path_for_prefers_the_environment(self) -> None:
        custom = os.path.join(self.directory.name, "elsewhere.db")
        with mock.patch.dict(os.environ, {"PLUGGY_SNAPSHOT": custom}):
            self.assertEqual(snapshot_path_for(self.candidate), custom)


if __name__ == "__main__":
    unittest.main()
