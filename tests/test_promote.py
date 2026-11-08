"""Promotion: six steps under both locks, crash-safe at every seam, never a
divergence silently absorbed."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from pluggysync.ledger import classify, open_ledger
from pluggysync.legacy_import import import_states
from pluggysync.promote import (
    PromotionRefused,
    legacy_backup_path,
    promote,
    staging_path,
)
from pluggysync.run import LedgerRun
from pluggysync.walk import Client
from tests.fakes import FakeClock, FakeTransport
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, script_two_items
from tests.test_legacy_import import build_state, edit_state

NOW = "2026-11-08T12:00:00+00:00"


class Crash(BaseException):
    pass


def crash_after(step: str):
    def hook(name: str, **context: object) -> None:
        if name == step:
            raise Crash(step)

    return hook


def clean(path: str) -> list[str]:
    return []


def table_count(path: str, table: str) -> int:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        db.close()


class PromoteCase(unittest.TestCase):
    """A legacy live file equal to the November fixture, and a candidate built
    from the fixture pair with one live run on top."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = self.directory.name
        self.sep, self.nov = os.path.join(root, "sep.db"), os.path.join(root, "nov.db")
        build_state(self.sep, "legacy_sep.sql")
        build_state(self.nov, "legacy_nov.sql")
        self.live = os.path.join(root, "pluggy.db")
        shutil.copyfile(self.nov, self.live)
        self.candidate = self.build_candidate(os.path.join(root, "candidate.db"))
        self.states = [self.sep, self.nov]

    def build_candidate(self, path: str) -> str:
        db = open_ledger(path)
        import_states(db, [self.sep, self.nov])
        clock = FakeClock()
        clock.t = 400 * 24 * 3600  # well after the frozen states
        transport = FakeTransport(clock)
        script_two_items(transport)

        def make_client(on_attempt) -> Client:
            return Client("id", "secret", on_attempt, transport, clock)

        LedgerRun(db, [ITEM_A, ITEM_B], make_client, clock, "test").execute()
        db.close()
        return path

    def promote(self, **kw) -> object:
        return promote(self.live, self.candidate, self.states, kw.pop("check", clean), NOW, **kw)


class SixStepsTest(PromoteCase):
    def test_the_six_steps_in_order_leave_a_checked_cutover_marked_live_file(self) -> None:
        seen: list[str] = []
        checked: list[str] = []

        def check(path: str) -> list[str]:
            checked.append(os.path.basename(path))
            return []

        report = self.promote(check=check, crash_at=lambda name, **c: seen.append(name))

        self.assertEqual(
            seen, ["after_step_1", "after_step_2", "after_step_3", "after_step_4", "after_step_5"]
        )
        self.assertEqual([s[:1] for s in report.steps], ["1", "2", "3", "4", "5", "6"])  # type: ignore[attr-defined]
        self.assertEqual(checked, [".pluggy.db.promote-staging", "pluggy.db"])
        self.assertEqual(classify(self.live), "ledger")
        live = sqlite3.connect(f"file:{self.live}?mode=ro", uri=True)
        self.addCleanup(live.close)
        cutover = live.execute(
            "SELECT completed_at, from_states, acceptance_run FROM cutover"
        ).fetchone()
        self.assertEqual(cutover, (NOW, '["sep.db", "nov.db"]', 4))
        self.assertEqual(table_count(self.live, "runs"), 4)
        backup = legacy_backup_path(self.live)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)
        self.assertEqual(classify(backup), "legacy")
        self.assertEqual(table_count(backup, "transactions"), 3)
        self.assertEqual(os.stat(self.live).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(staging_path(self.live)))
        self.assertEqual(table_count(self.candidate, "cutover"), 0)

    def test_a_candidate_on_another_filesystem_promotes_through_the_staging_copy(self) -> None:
        if not os.access("/dev/shm", os.W_OK):
            self.skipTest("no writable tmpfs at /dev/shm")
        elsewhere = tempfile.mkdtemp(dir="/dev/shm")
        self.addCleanup(shutil.rmtree, elsewhere)
        moved = os.path.join(elsewhere, "candidate.db")
        shutil.copyfile(self.candidate, moved)
        self.candidate = moved

        self.promote()

        self.assertEqual(classify(self.live), "ledger")
        self.assertTrue(os.path.exists(moved))


class DivergenceTest(PromoteCase):
    def assert_aborts_at_step_1(self, fragment: str) -> None:
        with self.assertRaises(PromotionRefused) as refused:
            self.promote()
        self.assertIn(fragment, str(refused.exception))
        self.assertFalse(os.path.exists(legacy_backup_path(self.live)))
        self.assertFalse(os.path.exists(staging_path(self.live)))
        self.assertEqual(classify(self.live), "legacy")

    def test_an_extra_legacy_run_aborts(self) -> None:
        edit_state(
            self.live,
            "INSERT INTO sync_runs (id, started_at, finished_at, api_calls)"
            " VALUES (4, '2026-11-08T10:00:00+00:00', '2026-11-08T10:00:10+00:00', 7)",
        )
        self.assert_aborts_at_step_1("sync_runs")

    def test_a_modified_probe_row_aborts(self) -> None:
        edit_state(self.live, "UPDATE probes SET rows = 99 WHERE rowid = 3")
        self.assert_aborts_at_step_1("probes")

    def test_a_modified_resource_row_aborts(self) -> None:
        edit_state(
            self.live,
            "UPDATE transactions SET raw = replace(raw, '-10.0', '-11.0') WHERE id = 'tx-0001'",
        )
        self.assert_aborts_at_step_1("transactions rows")

    def test_an_existing_legacy_backup_is_never_overwritten(self) -> None:
        with open(legacy_backup_path(self.live), "wb") as planted:
            planted.write(b"keep me")

        with self.assertRaises(PromotionRefused) as refused:
            self.promote()

        self.assertIn("never overwritten", str(refused.exception))
        with open(legacy_backup_path(self.live), "rb") as planted:
            self.assertEqual(planted.read(), b"keep me")
        self.assertEqual(classify(self.live), "legacy")

    def test_a_staged_candidate_that_fails_check_is_not_promoted(self) -> None:
        with self.assertRaises(PromotionRefused) as refused:
            self.promote(check=lambda path: ["one problem"])

        self.assertIn("fails --check", str(refused.exception))
        self.assertEqual(classify(self.live), "legacy")
        self.assertFalse(os.path.exists(staging_path(self.live)))


class CrashTest(PromoteCase):
    def test_a_crash_before_the_rename_is_finished_by_the_next_promotion(self) -> None:
        with self.assertRaises(Crash):
            self.promote(crash_at=crash_after("after_step_4"))
        self.assertEqual(classify(self.live), "legacy")
        self.assertTrue(os.path.exists(staging_path(self.live)))
        self.assertEqual(table_count(staging_path(self.live), "cutover"), 1)
        checked: list[str] = []

        report = self.promote(check=lambda path: checked.append(path) or [])

        self.assertTrue(report.resumed)  # type: ignore[attr-defined]
        self.assertEqual(classify(self.live), "ledger")
        self.assertEqual(table_count(self.live, "cutover"), 1)
        self.assertEqual(checked, [self.live])
        self.assertFalse(os.path.exists(staging_path(self.live)))

    def test_a_crash_after_the_rename_leaves_a_cutover_marked_default(self) -> None:
        with self.assertRaises(Crash):
            self.promote(crash_at=crash_after("after_step_5"))

        self.assertEqual(classify(self.live), "ledger")
        self.assertEqual(table_count(self.live, "cutover"), 1)
        self.assertFalse(os.path.exists(staging_path(self.live)))
        self.assertTrue(os.path.exists(legacy_backup_path(self.live)))

    def test_a_crash_after_the_backup_leaves_the_backup_and_no_staging(self) -> None:
        with self.assertRaises(Crash):
            self.promote(crash_at=crash_after("after_step_2"))

        self.assertEqual(classify(self.live), "legacy")
        self.assertTrue(os.path.exists(legacy_backup_path(self.live)))
        self.assertFalse(os.path.exists(staging_path(self.live)))
        # The next attempt refuses rather than overwrite the backup it already made.
        with self.assertRaises(PromotionRefused):
            self.promote()


if __name__ == "__main__":
    unittest.main()
