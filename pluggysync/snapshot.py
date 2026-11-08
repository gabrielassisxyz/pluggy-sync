"""A consistent copy of the ledger for the backup job to pick up.

Copying a live SQLite file is not atomic; `Connection.backup` is. Every sync
run ends by writing one through this module, so the nightly job only ever
sees a complete file. The snapshot is derived: the program never reads it,
and deleting it loses nothing.
"""

from __future__ import annotations

import os

from .promote import _verified_copy

SNAPSHOT_NAME = "pluggy-snapshot.db"
SNAPSHOT_ENV = "PLUGGY_SNAPSHOT"


class SnapshotError(Exception):
    """The snapshot could not be written; the previous file is left intact."""


def snapshot_path_for(db_path: str) -> str:
    """The snapshot path for a run that wrote `db_path`."""
    override = os.environ.get(SNAPSHOT_ENV)
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), SNAPSHOT_NAME)


def write_snapshot(db_path: str, snapshot_path: str) -> str:
    """Write a verified copy of `db_path` onto `snapshot_path`.

    The copy goes to a `0600` temporary file in the snapshot's directory,
    passes `PRAGMA integrity_check`, and is then renamed over the target, so
    a failure never leaves a half-written snapshot behind.
    """
    directory = os.path.dirname(os.path.abspath(snapshot_path))
    if not os.path.isdir(directory):
        raise SnapshotError(f"snapshot directory does not exist: {directory}")
    try:
        temporary = _verified_copy(db_path, directory, ".pluggy-snapshot-")
    except Exception as failure:
        raise SnapshotError(f"snapshot of {db_path} failed: {failure}") from failure
    try:
        os.replace(temporary, snapshot_path)
    except OSError as failure:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise SnapshotError(f"snapshot of {db_path} failed: {failure}") from failure
    try:
        os.chmod(snapshot_path, 0o600)
    except OSError as failure:
        raise SnapshotError(f"snapshot of {db_path} failed: {failure}") from failure
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as failure:
        raise SnapshotError(f"snapshot of {db_path} failed: {failure}") from failure
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return snapshot_path
