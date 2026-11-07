"""`--promote <candidate> --from-states <state> ...`: the checked candidate becomes
the live database, under a crash-safe sequence.

A filesystem rename is outside any SQLite transaction and `os.replace` across
filesystems fails with EXDEV, so the candidate is first copied to a staging
file beside the live one, marked with its `cutover` row, checked, and only then
renamed over the live path. Every step before the rename leaves the legacy
file as the default; a crash after it leaves a cutover-marked default. A
staging file already carrying its mark is a promotion to finish, not to redo.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from .ledger import classify, transaction
from .lock import ProcessLock
from .projections import PROJECTION_NAMES

Check = Callable[[str], list[str]]
CrashHook = Callable[..., None]


class PromotionRefused(Exception):
    pass


@dataclass
class PromotionReport:
    steps: list[str] = field(default_factory=list)
    legacy_backup: str | None = None
    resumed: bool = False


def staging_path(live_path: str) -> str:
    directory, name = os.path.split(os.path.abspath(live_path))
    return os.path.join(directory, f".{name}.promote-staging")


def legacy_backup_path(live_path: str) -> str:
    return f"{live_path}-legacy-final"


def _readonly(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _fingerprint(connection: sqlite3.Connection, table: str) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(f"SELECT id, raw, fetched_at FROM {table} ORDER BY id"):
        digest.update(json.dumps(row).encode())
    return digest.hexdigest()


def compare_live_to_state(live_path: str, state_path: str) -> None:
    """Step 1: the live legacy file must equal the last frozen state, control
    tables row for row and every resource table by fingerprint. A legacy run
    made after the freeze has to be frozen as a state first."""
    if classify(live_path) != "legacy":
        raise PromotionRefused(f"{live_path} is not a legacy database ({classify(live_path)})")
    live, state = _readonly(live_path), _readonly(state_path)
    try:
        for table, order in (("sync_runs", "id"), ("probes", "rowid")):
            if (
                live.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
                != state.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            ):
                raise PromotionRefused(
                    f"{table} of {live_path} diverges from {state_path}: a legacy run after the"
                    " freeze must be frozen as a state and the candidate rebuilt from all states"
                )
        for table in PROJECTION_NAMES:
            if _fingerprint(live, table) != _fingerprint(state, table):
                raise PromotionRefused(f"{table} rows of {live_path} diverge from {state_path}")
    finally:
        live.close()
        state.close()


def _verified_copy(source: str, directory: str, prefix: str) -> str:
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=prefix)
    os.close(fd)
    origin, copy = sqlite3.connect(source), sqlite3.connect(temporary)
    try:
        origin.backup(copy)
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        copy.close()
        origin.close()
    if verdict != "ok":
        os.unlink(temporary)
        raise PromotionRefused(f"copy of {source} failed integrity_check: {verdict}")
    with open(temporary, "rb") as handle:
        os.fsync(handle.fileno())
    return temporary


def _fsync_directory(path: str) -> None:
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_legacy(live_path: str) -> str:
    """Step 2: `<live>-legacy-final`, published by exclusive create, never overwritten."""
    target = legacy_backup_path(live_path)
    if os.path.exists(target):
        raise PromotionRefused(f"{target} already exists; it is never overwritten")
    temporary = _verified_copy(
        live_path, os.path.dirname(os.path.abspath(live_path)), ".legacy-final-"
    )
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise PromotionRefused(f"{target} appeared while the backup was being written") from None
    finally:
        os.unlink(temporary)
    _fsync_directory(target)
    return target


def stage_candidate(candidate_path: str, live_path: str) -> str:
    """Step 3: a verified 0600 copy of the candidate in the live file's directory."""
    target = staging_path(live_path)
    temporary = _verified_copy(candidate_path, os.path.dirname(target), ".promote-")
    os.replace(temporary, target)
    _fsync_directory(target)
    return target


def acceptance_run_of(candidate_path: str) -> int:
    connection = _readonly(candidate_path)
    try:
        row = connection.execute(
            "SELECT max(id) FROM runs WHERE outcome = 'complete' AND code_version <> 'unknown'"
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None:
        raise PromotionRefused(f"{candidate_path} carries no complete live run to accept")
    return int(row[0])


def mark_cutover(
    staging: str, state_paths: list[str], acceptance_run: int, completed_at: str
) -> None:
    """Step 4: the row the default sync looks for before it will write."""
    connection = sqlite3.connect(staging, isolation_level=None)
    try:
        with transaction(connection):
            connection.execute(
                "INSERT INTO cutover (id, completed_at, from_states, acceptance_run)"
                " VALUES (1, ?, ?, ?)",
                (
                    completed_at,
                    json.dumps([os.path.basename(p) for p in state_paths]),
                    acceptance_run,
                ),
            )
    finally:
        connection.close()


def _is_marked(staging: str) -> bool:
    if not os.path.exists(staging):
        return False
    connection = _readonly(staging)
    try:
        return bool(connection.execute("SELECT count(*) FROM cutover").fetchone()[0])
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def promote(
    live_path: str,
    candidate_path: str,
    state_paths: list[str],
    check: Check,
    completed_at: str,
    crash_at: CrashHook | None = None,
) -> PromotionReport:
    """The six steps, under the write locks of both files, held throughout."""
    hook = crash_at or (lambda name, **context: None)
    if not state_paths:
        raise PromotionRefused("--from-states names the frozen states the candidate was built from")
    if os.path.realpath(live_path) == os.path.realpath(candidate_path):
        raise PromotionRefused("the candidate is the live database")
    report = PromotionReport()
    live_lock = ProcessLock.exclusive(live_path)
    candidate_lock = ProcessLock.exclusive(candidate_path)
    try:
        staging = staging_path(live_path)
        if _is_marked(staging):
            report.resumed = True
            report.steps.append("resume: a marked staging file exists; finishing from step 5")
        else:
            compare_live_to_state(live_path, state_paths[-1])
            report.steps.append("1 live file equals the last frozen state")
            hook("after_step_1")
            report.legacy_backup = backup_legacy(live_path)
            report.steps.append(f"2 legacy backup at {report.legacy_backup}")
            hook("after_step_2")
            stage_candidate(candidate_path, live_path)
            report.steps.append(f"3 candidate staged at {staging}")
            hook("after_step_3")
            mark_cutover(staging, state_paths, acceptance_run_of(candidate_path), completed_at)
            problems = check(staging)
            if problems:
                os.unlink(staging)
                raise PromotionRefused(f"the staged candidate fails --check: {'; '.join(problems)}")
            report.steps.append("4 cutover row written and staging file checked")
            hook("after_step_4")
        os.replace(staging, live_path)
        _fsync_directory(live_path)
        os.chmod(live_path, 0o600)
        report.steps.append("5 staging file renamed onto the live path")
        hook("after_step_5")
        problems = check(live_path)
        if problems:
            raise PromotionRefused(f"the promoted file fails --check: {'; '.join(problems)}")
        report.steps.append("6 promoted file checked")
        return report
    finally:
        candidate_lock.release()
        live_lock.release()
