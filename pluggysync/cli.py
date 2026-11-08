"""Command line: which database, which mode, and the refusals that keep a
candidate off the live file.

Until the cutover, the default invocation is still the legacy sync. The ledger
runs only against a candidate database named with `--candidate`, which must be
neither the live file nor an alias of it, and never an unignored path inside
the repository: the file holds a person's financial history.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence

from .check import check_connection, check_file
from .events import ProjectionBlocked
from .ledger import LegacyDatabase, UnknownDatabase, open_ledger
from .legacy_import import ImportFailed, import_states
from .lock import LockHeld, LockRefused, ProcessLock
from .promote import PromotionRefused, promote
from .rebuild import rebuild
from .reparse import ReparseRefused, reparse
from .run import ConfigurationError, LedgerRun, code_version, parse_item_ids, self_check
from .snapshot import SnapshotError, snapshot_path_for, write_snapshot
from .transport import OnAttempt
from .walk import Client, Clock, SystemClock, utcnow

MakeClient = Callable[[OnAttempt], Client]


class CandidateRefused(Exception):
    pass


class CutoverMissing(Exception):
    """A ledger file nobody promoted: an intermediate state the default sync refuses."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"{path} is a ledger database without a cutover row, so it was never promoted;"
            " run the ledger against a candidate with --candidate <path>, or install a checked"
            " candidate with --promote <path> --from-states ..."
        )


def script_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def live_db_path() -> str:
    return os.environ.get("PLUGGY_DB") or os.path.join(script_dir(), "pluggy.db")


def check_candidate_path(path: str, live_path: str) -> str:
    """The resolved candidate path, or `CandidateRefused` with the reason."""
    resolved = os.path.realpath(path)
    live = os.path.realpath(live_path)
    if resolved == live:
        raise CandidateRefused(f"{path} is the live database")
    if os.path.exists(resolved) and os.path.exists(live) and os.path.samefile(resolved, live):
        raise CandidateRefused(f"{path} is the live database under another name")
    directory = os.path.dirname(resolved)
    if not os.path.isdir(directory):
        raise CandidateRefused(f"{directory} does not exist")
    inside = subprocess.run(
        ["git", "-C", directory, "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if inside.returncode == 0:
        ignored = subprocess.run(
            ["git", "-C", directory, "check-ignore", "-q", resolved], capture_output=True
        )
        if ignored.returncode != 0:
            raise CandidateRefused(
                f"{path} is inside the repository {inside.stdout.strip()} and git does not"
                " ignore it; a database never goes into version control"
            )
    return resolved


def _say(line: str) -> None:
    print(line, file=sys.stderr)


def _environment() -> tuple[str, str, list[str]]:
    client_id = os.environ.get("PLUGGY_CLIENT_ID") or ""
    client_secret = os.environ.get("PLUGGY_CLIENT_SECRET") or ""
    raw_items = os.environ.get("PLUGGY_ITEM_IDS") or ""
    missing = [
        name
        for name, value in (
            ("PLUGGY_CLIENT_ID", client_id),
            ("PLUGGY_CLIENT_SECRET", client_secret),
            ("PLUGGY_ITEM_IDS", raw_items),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"missing in the environment: {', '.join(missing)} (see README: Running it)"
        )
    return client_id, client_secret, parse_item_ids(raw_items)


def _open(path: str) -> tuple[ProcessLock, sqlite3.Connection]:
    os.umask(0o077)
    lock = ProcessLock.exclusive(path)
    try:
        connection = open_ledger(path)
    except BaseException:
        lock.release()
        raise
    return lock, connection


def ledger_sync(
    path: str,
    make_client: MakeClient | None,
    clock: Clock | None,
    require_cutover: bool = False,
) -> int:
    client_id, client_secret, item_ids = _environment()
    clock = clock or SystemClock()
    lock, connection = _open(path)
    try:
        if require_cutover and not connection.execute("SELECT count(*) FROM cutover").fetchone()[0]:
            raise CutoverMissing(path)

        def default_client(on_attempt: OnAttempt) -> Client:
            return Client(client_id, client_secret, on_attempt, clock=clock)

        run = LedgerRun(
            connection, item_ids, make_client or default_client, clock, code_version(script_dir())
        )
        print(f"database: {path}")
        swept = run.sweep()
        for run_id in swept:
            print(f"run {run_id}: left running by a crash, closed as interrupted")
        run_id = run.execute()
        row = connection.execute(
            "SELECT outcome, api_calls, expected, complete, unavailable, incomplete, skipped,"
            " ambiguous_documents FROM run_coverage WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        outcome, api_calls, expected, complete, unavailable, incomplete, skipped, ambiguous = row
        print(
            f"run {run_id}: {outcome}; {api_calls} API calls; {expected} walks owed:"
            f" {complete} complete, {unavailable} unavailable, {incomplete} incomplete,"
            f" {skipped} skipped; {ambiguous} ambiguous documents"
        )
        snapshot_failed = False
        if self_check(connection, run_id):
            _say(f"run {run_id}: self-check reports problems; keeping the previous snapshot")
        else:
            try:
                write_snapshot(path, snapshot_path_for(path))
            except SnapshotError as failure:
                _say(str(failure))
                snapshot_failed = True
        if outcome == "failed" or snapshot_failed:
            return 1
        return 0
    finally:
        connection.close()
        lock.release()


def import_mode(path: str, state_paths: list[str]) -> int:
    """The historical import: the frozen legacy states become the candidate's
    first runs. A failed import leaves no candidate behind."""
    existed = os.path.exists(path)
    lock, connection = _open(path)
    try:
        report = import_states(connection, state_paths)
        print(
            f"{path}: imported {len(state_paths)} state(s): {report.runs} runs,"
            f" {report.scopes} walks, {report.sightings} sightings of"
            f" {report.documents} documents"
        )
        for remark in report.remarks:
            print(f"  note: {remark}")
        return 0
    except ImportFailed:
        connection.close()
        lock.release()
        if not existed:
            os.unlink(path)
        raise
    finally:
        try:
            connection.close()
        except sqlite3.ProgrammingError:
            pass
        lock.release()


def rebuild_mode(path: str) -> int:
    lock, connection = _open(path)
    try:
        replayed = rebuild(connection)
        print(f"{path}: layer 3 rebuilt from {len(replayed)} runs")
        return 0
    finally:
        connection.close()
        lock.release()


def _check_under_our_lock(path: str) -> list[str]:
    """The integrity check for a file this process already holds the write lock on:
    `check_file` would take the shared lock and read its own caller as a writer."""
    if (os.stat(path).st_mode & 0o777) != 0o600:
        return [f"file mode {oct(os.stat(path).st_mode & 0o777)} is not 0600"]
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        problems, _ = check_connection(connection)
    finally:
        connection.close()
    return problems


def promote_mode(candidate: str, state_paths: list[str]) -> int:
    live = live_db_path()
    report = promote(
        live,
        candidate,
        state_paths,
        check=_check_under_our_lock,
        completed_at=utcnow(),
    )
    for step in report.steps:
        print(step)
    print(f"{live}: promoted from {candidate}")
    return 0


def check_mode(path: str) -> int:
    code, problems, info = check_file(path)
    for line in info:
        print(line)
    if code == 0:
        print(f"{path}: check clean")
        return 0
    for line in problems:
        _say(line)
    return 1


def reparse_mode(source: str, dest: str, retime: bool) -> int:
    reparse(source, dest, retime=retime)
    print(f"{dest}: reparsed from {source}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    make_client: MakeClient | None = None,
    clock: Clock | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="pluggy_sync.py",
        description="Pull everything a Pluggy account will hand over into one local SQLite file.",
    )
    parser.add_argument(
        "--candidate",
        metavar="PATH",
        help="run the ledger sync against a dedicated candidate database instead of the"
        " legacy sync against PLUGGY_DB",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="regenerate events, states and current-state tables from the ledger",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report every inconsistency between the archive, the ledger and the projections",
    )
    parser.add_argument(
        "--reparse",
        metavar="PATH",
        help="rebuild a corrected copy of the whole history from the response archive",
    )
    parser.add_argument(
        "--retime",
        action="store_true",
        help="recompute local days under the current time rules during --reparse",
    )
    parser.add_argument(
        "--from-states",
        metavar="STATE",
        nargs="+",
        help="with --candidate: build the candidate from these frozen legacy databases,"
        " oldest first, instead of running a sync",
    )
    parser.add_argument(
        "--promote",
        metavar="CANDIDATE",
        help="with --from-states: make this checked candidate the live database",
    )
    args = parser.parse_args(argv)
    if args.promote:
        if not args.from_states:
            _say("--promote needs --from-states <state> ...: the states the candidate came from")
            return 2
        if args.candidate or args.check or args.reparse or args.rebuild or args.retime:
            _say("--promote takes only --from-states")
            return 2
        try:
            candidate = check_candidate_path(args.promote, live_db_path())
            return promote_mode(candidate, args.from_states)
        except (CandidateRefused, ConfigurationError) as refused:
            _say(str(refused))
            return 2
        except (LockHeld, LockRefused, PromotionRefused) as refused:
            _say(str(refused))
            return 1
    if args.from_states and not args.candidate:
        _say("--from-states needs --candidate <path>: the import never touches the live database")
        return 2
    if args.from_states and (args.check or args.reparse or args.rebuild):
        _say("--from-states cannot be combined with --check, --reparse or --rebuild")
        return 2
    modes = (args.candidate, args.rebuild, args.check, args.reparse, args.retime, args.from_states)
    try:
        if not any(modes):
            return ledger_sync(live_db_path(), make_client, clock, require_cutover=True)
    except ConfigurationError as refused:
        _say(str(refused))
        return 2
    except (
        LockHeld,
        LockRefused,
        LegacyDatabase,
        UnknownDatabase,
        CutoverMissing,
        ProjectionBlocked,
    ) as refused:
        _say(str(refused))
        return 1
    if args.retime and not args.reparse:
        _say("--retime needs --reparse")
        return 2
    if args.check and args.reparse:
        _say("--check and --reparse cannot be combined")
        return 2
    if args.rebuild and (args.check or args.reparse):
        _say("--rebuild cannot be combined with --check or --reparse")
        return 2
    try:
        if args.reparse:
            source = (
                check_candidate_path(args.candidate, live_db_path())
                if args.candidate
                else live_db_path()
            )
            dest = check_candidate_path(args.reparse, live_db_path())
            return reparse_mode(source, dest, args.retime)
        path = (
            check_candidate_path(args.candidate, live_db_path())
            if args.candidate
            else live_db_path()
        )
        if args.from_states:
            return import_mode(path, args.from_states)
        if args.check:
            return check_mode(path)
        if args.rebuild:
            return rebuild_mode(path)
        return ledger_sync(path, make_client, clock)
    except (CandidateRefused, ConfigurationError, ReparseRefused) as refused:
        _say(str(refused))
        return 2
    except (
        LockHeld,
        LockRefused,
        LegacyDatabase,
        UnknownDatabase,
        ProjectionBlocked,
        ImportFailed,
    ) as refused:
        _say(str(refused))
        return 1
    except RuntimeError as failed:
        _say(str(failed))
        return 1
