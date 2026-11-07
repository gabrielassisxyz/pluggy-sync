"""The migration framework: a verified backup published by exclusive create,
reused only when byte-identical, and a version bump inside the migration's own
transaction."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from pluggysync.ledger import (
    MIGRATIONS,
    BackupConflict,
    Migration,
    backup_path,
    open_ledger,
    user_version,
)


def add_notes_table(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")


def add_notes_table_then_fail(connection: sqlite3.Connection) -> None:
    add_notes_table(connection)
    raise RuntimeError("injected failure before commit")


VERSION_2 = (*MIGRATIONS, Migration(2, add_notes_table))
VERSION_2_FAILING = (*MIGRATIONS, Migration(2, add_notes_table_then_fail))


def version_of(path: str) -> int:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return user_version(db)
    finally:
        db.close()


def tables_of(path: str) -> set[str]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def file_modes(directory: str) -> dict[str, int]:
    return {
        name: os.stat(os.path.join(directory, name)).st_mode & 0o777
        for name in os.listdir(directory)
    }


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "ledger.db")
        fixture = open_ledger(self.path, MIGRATIONS)
        fixture.execute("BEGIN IMMEDIATE")
        fixture.execute(
            "INSERT INTO runs (started_at, started_day, outcome, code_version, item_ids)"
            " VALUES ('2026-01-05T03:00:00+00:00','2026-01-05','running','test','[]')"
        )
        fixture.execute("COMMIT")
        fixture.close()
        self.backup = backup_path(self.path, 2)

    def test_migration_writes_a_verified_backup_then_bumps_the_version(self) -> None:
        db = open_ledger(self.path, VERSION_2)
        db.close()

        self.assertEqual(version_of(self.path), 2)
        self.assertIn("notes", tables_of(self.path))
        self.assertTrue(os.path.exists(self.backup))
        backup = sqlite3.connect(f"file:{self.backup}?mode=ro", uri=True)
        self.addCleanup(backup.close)
        self.assertEqual(user_version(backup), 1)
        self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(backup.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertNotIn("notes", tables_of(self.backup))

    def test_failure_before_commit_rolls_back_and_the_backup_is_reused_afterwards(self) -> None:
        with self.assertRaises(RuntimeError):
            open_ledger(self.path, VERSION_2_FAILING)

        self.assertEqual(version_of(self.path), 1)
        self.assertNotIn("notes", tables_of(self.path))
        self.assertTrue(os.path.exists(self.backup))
        first_inode = os.stat(self.backup).st_ino

        db = open_ledger(self.path, VERSION_2)
        db.close()

        self.assertEqual(version_of(self.path), 2)
        self.assertEqual(os.stat(self.backup).st_ino, first_inode)

    def test_a_differing_file_at_the_backup_path_aborts_without_overwriting(self) -> None:
        with open(self.backup, "wb") as planted:
            planted.write(b"not a backup")

        with self.assertRaises(BackupConflict):
            open_ledger(self.path, VERSION_2)

        with open(self.backup, "rb") as planted:
            self.assertEqual(planted.read(), b"not a backup")
        self.assertEqual(version_of(self.path), 1)

    def test_a_file_appearing_between_check_and_publish_aborts_without_overwriting(self) -> None:
        def plant() -> None:
            with open(self.backup, "wb") as planted:
                planted.write(b"raced in")

        with self.assertRaises(BackupConflict):
            open_ledger(self.path, VERSION_2, before_publish=plant)

        with open(self.backup, "rb") as planted:
            self.assertEqual(planted.read(), b"raced in")
        self.assertEqual(version_of(self.path), 1)
        self.assertEqual(
            sorted(os.listdir(self.directory.name)), ["ledger.db", os.path.basename(self.backup)]
        )

    def test_every_file_the_program_created_is_private(self) -> None:
        db = open_ledger(self.path, VERSION_2)
        db.close()

        modes = file_modes(self.directory.name)
        self.assertEqual(set(modes), {"ledger.db", os.path.basename(self.backup)})
        self.assertEqual(set(modes.values()), {0o600}, modes)

    def test_a_newer_schema_than_the_code_is_refused(self) -> None:
        db = open_ledger(self.path, VERSION_2)
        db.close()

        with self.assertRaises(Exception) as refused:
            open_ledger(self.path, MIGRATIONS)
        self.assertIn("newer", str(refused.exception))

    def test_a_new_file_is_created_at_the_latest_version_without_a_backup(self) -> None:
        fresh = os.path.join(self.directory.name, "fresh.db")

        db = open_ledger(fresh, VERSION_2)
        db.close()

        self.assertEqual(version_of(fresh), 2)
        self.assertFalse(os.path.exists(backup_path(fresh, 1)))
        self.assertFalse(os.path.exists(backup_path(fresh, 2)))


if __name__ == "__main__":
    unittest.main()
