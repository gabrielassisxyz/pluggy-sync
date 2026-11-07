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

from .check import check_file
from .events import ProjectionBlocked
from .ledger import LegacyDatabase, UnknownDatabase, open_ledger
from .lock import LockHeld, LockRefused, ProcessLock
from .rebuild import rebuild
from .reparse import ReparseRefused, reparse
from .run import ConfigurationError, LedgerRun, code_version, parse_item_ids
from .transport import OnAttempt
from .walk import Client, Clock, SystemClock

MakeClient = Callable[[OnAttempt], Client]


class CandidateRefused(Exception):
    pass


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


def ledger_sync(path: str, make_client: MakeClient | None, clock: Clock | None) -> int:
    client_id, client_secret, item_ids = _environment()
    clock = clock or SystemClock()
    lock, connection = _open(path)
    try:

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
        return 0 if outcome != "failed" else 1
    finally:
        connection.close()
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
    args = parser.parse_args(argv)
    if not any((args.candidate, args.rebuild, args.check, args.reparse, args.retime)):
        from pluggy_sync import sync  # the legacy path, until cutover

        return sync()
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
        if args.check:
            return check_mode(path)
        if args.rebuild:
            return rebuild_mode(path)
        return ledger_sync(path, make_client, clock)
    except (CandidateRefused, ConfigurationError, ReparseRefused) as refused:
        _say(str(refused))
        return 2
    except (LockHeld, LockRefused, LegacyDatabase, UnknownDatabase, ProjectionBlocked) as refused:
        _say(str(refused))
        return 1
    except RuntimeError as failed:
        _say(str(failed))
        return 1
