"""One writer per database file, across processes and across spellings of the path."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from pluggysync.lock import LockHeld, LockRefused, ProcessLock, lock_path

ROOT = pathlib.Path(__file__).resolve().parents[1]

HOLDER = """
import sys, time
from pluggysync.lock import ProcessLock
lock = ProcessLock.exclusive(sys.argv[1])
print("held", flush=True)
time.sleep(60)
"""


def stop(holder: subprocess.Popen[str]) -> None:
    holder.kill()
    holder.wait(timeout=5)
    if holder.stdout is not None:
        holder.stdout.close()


def hold_in_another_process(db_path: str) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, db_path],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    line = holder.stdout.readline()
    if line.strip() != "held":
        stop(holder)
        raise AssertionError(f"holder did not take the lock: {line!r}")
    return holder


class ProcessLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = os.path.join(self.directory.name, "ledger.db")

    def test_second_process_is_refused_within_a_second(self) -> None:
        holder = hold_in_another_process(self.db_path)
        self.addCleanup(stop, holder)

        started = time.monotonic()
        with self.assertRaises(LockHeld) as refused:
            ProcessLock.exclusive(self.db_path)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(refused.exception.path, lock_path(self.db_path))

    def test_lock_is_released_when_the_holder_is_killed(self) -> None:
        holder = hold_in_another_process(self.db_path)
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)
        self.addCleanup(stop, holder)

        lock = ProcessLock.exclusive(self.db_path)
        lock.release()

    def test_relative_and_absolute_spellings_contend_for_one_lock(self) -> None:
        holder = hold_in_another_process(self.db_path)
        self.addCleanup(stop, holder)
        previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, previous)

        with self.assertRaises(LockHeld):
            ProcessLock.exclusive("ledger.db")

    def test_shared_locks_coexist_but_exclude_a_writer(self) -> None:
        reader = ProcessLock.shared(self.db_path)
        self.addCleanup(reader.release)
        another_reader = ProcessLock.shared(self.db_path)
        another_reader.release()

        holder = subprocess.run(
            [sys.executable, "-c", HOLDER, self.db_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(holder.returncode, 0)
        self.assertIn("LockHeld", holder.stderr)

    def test_lock_file_is_private(self) -> None:
        lock = ProcessLock.exclusive(self.db_path)
        self.addCleanup(lock.release)

        self.assertEqual(os.stat(lock.path).st_mode & 0o777, 0o600)

    def test_a_hard_linked_lock_file_is_refused(self) -> None:
        path = lock_path(self.db_path)
        with open(path, "w"):
            pass
        os.link(path, path + ".alias")

        with self.assertRaises(LockRefused):
            ProcessLock.exclusive(self.db_path)

    def test_a_symlinked_lock_path_is_refused(self) -> None:
        elsewhere = os.path.join(self.directory.name, "elsewhere")
        with open(elsewhere, "w"):
            pass
        os.symlink(elsewhere, lock_path(self.db_path))

        with self.assertRaises(OSError):
            ProcessLock.exclusive(self.db_path)


class LegacySyncTakesTheLockTest(unittest.TestCase):
    def test_legacy_sync_exits_non_zero_before_its_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "pluggy.db")
            holder = hold_in_another_process(db_path)
            self.addCleanup(stop, holder)
            env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
            env.update(
                PLUGGY_DB=db_path,
                PLUGGY_CLIENT_ID="placeholder",
                PLUGGY_CLIENT_SECRET="placeholder",
                PLUGGY_ITEM_IDS="item-0001",
            )

            run = subprocess.run(
                [sys.executable, str(ROOT / "pluggy_sync.py")],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("another process holds", run.stderr)
            self.assertFalse(os.path.exists(db_path))


if __name__ == "__main__":
    unittest.main()
