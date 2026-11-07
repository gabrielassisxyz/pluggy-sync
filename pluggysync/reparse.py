"""`--reparse`: a corrected copy of the whole history from the response archive.

The source is never written to. Responses, legacy tables and the cutover
provenance are copied verbatim; migrated runs keep layer 2 as is; every
archived run is re-derived under the current parser with its original
identity, then layer 3 is rebuilt, checked, and validated against the
acceptance run before the result is published by exclusive create.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Callable
from typing import Any

from .archive import archived_responses
from .derive import Scope as DeriveScope
from .derive import canonical, derive_page
from .ledger import local_day, open_ledger, transaction
from .lock import LockHeld, ProcessLock
from .rebuild import rebuild
from .run import CHILD_KINDS

CrashHook = Callable[..., None]


class ReparseRefused(Exception):
    pass


def _is_credit_doc(document: dict[str, Any]) -> bool:
    return document.get("type") == "CREDIT"


def _ambiguous_in_memory(
    scopes: list[tuple], pages_by_scope: dict[int, list]
) -> dict[tuple[str, str], tuple[list[str], str]]:
    per_key: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for scope in scopes:
        scope_id = int(scope[0])
        kind = str(scope[2])
        for _, valid, _, _, docs in pages_by_scope.get(scope_id, []):
            if not valid:
                continue
            for document in docs:
                identifier = document.get("id")
                if not isinstance(identifier, str) or not identifier:
                    continue
                sha, _ = canonical(document)
                per_key.setdefault((kind, identifier), []).append((sha, str(scope[3])))
    ambiguous: dict[tuple[str, str], tuple[list[str], str]] = {}
    for key, sightings in per_key.items():
        shas = {sha for sha, _ in sightings}
        parents = sorted({parent for _, parent in sightings})
        reasons = []
        if len(shas) > 1:
            reasons.append("two bodies in one run")
        if len(parents) > 1:
            reasons.append("two parents in one run")
        if reasons:
            ambiguous[key] = (parents, "; ".join(reasons))
    return ambiguous


def reparse(
    source_path: str,
    dest_path: str,
    retime: bool = False,
    crash_at: CrashHook | None = None,
    before_publish: Callable[[], None] | None = None,
) -> str:
    hook = crash_at or (lambda name, **context: None)
    if os.path.lexists(dest_path):
        raise ReparseRefused(f"{dest_path} already exists")
    try:
        source_lock = ProcessLock.shared(source_path)
    except LockHeld:
        raise RuntimeError(f"writer active: another process holds {source_path}") from None
    try:
        if not os.path.exists(source_path):
            raise RuntimeError(f"{source_path} does not exist")
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        try:
            source.execute("PRAGMA foreign_keys=ON")
            runs = source.execute(
                "SELECT id, started_at, started_day, finished_at, finished_day, outcome,"
                " api_calls, code_version, item_ids, note FROM runs ORDER BY id"
            ).fetchall()
            scopes_all = source.execute(
                "SELECT id, run_id, kind, parent, expected, outcome, infer_absence, status,"
                " rows, min_date, min_instant, closed_at, closed_day, error FROM scopes"
                " ORDER BY id"
            ).fetchall()
            response_rows_all = source.execute(
                "SELECT id, run_id, scope_id, method, url, status, headers, body_zlib,"
                " body_sha256, raw_bytes, truncated, error, received_at, received_day"
                " FROM responses ORDER BY id"
            ).fetchall()
            documents = source.execute("SELECT id, kind, provider_id FROM documents").fetchall()
            versions = source.execute("SELECT sha256, body FROM versions").fetchall()
            legacy_runs = source.execute("SELECT * FROM legacy_sync_runs").fetchall()
            legacy_probes = source.execute("SELECT * FROM legacy_probes").fetchall()
            cutover = source.execute(
                "SELECT id, completed_at, from_states, acceptance_run FROM cutover"
            ).fetchall()
            old_outcomes = {
                int(r[0]): str(r[1]) for r in source.execute("SELECT id, outcome FROM runs")
            }
            archived_by_scope: dict[int, list] = {}
            for scope in scopes_all:
                scope_id = int(scope[0])
                if any(r[2] == scope_id for r in response_rows_all):
                    archived_by_scope[scope_id] = archived_responses(source, scope_id)
            migrated_ids = {int(r[0]) for r in runs if str(r[5]) == "migrated"}
            migrated_observations: dict[int, list[tuple]] = {}
            for run_id in migrated_ids:
                migrated_observations[run_id] = source.execute(
                    "SELECT id, run_id, scope_id, response_id, position, document_id,"
                    " parent, sha256, observed_at, observed_day FROM observations"
                    " WHERE run_id = ? ORDER BY id",
                    (run_id,),
                ).fetchall()
        finally:
            source.close()

        directory = os.path.dirname(os.path.abspath(dest_path))
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".reparse-")
        os.close(fd)
        try:
            hook("after_temp", temporary=temporary)
            target = open_ledger(temporary)
            try:
                with transaction(target):
                    for doc_id, kind, provider_id in documents:
                        target.execute(
                            "INSERT INTO documents (id, kind, provider_id) VALUES (?,?,?)",
                            (doc_id, kind, provider_id),
                        )
                    for sha, body in versions:
                        target.execute(
                            "INSERT INTO versions (sha256, body) VALUES (?,?)"
                            " ON CONFLICT DO NOTHING",
                            (sha, body),
                        )
                    for row in legacy_runs:
                        marks = ", ".join("?" for _ in row)
                        target.execute(f"INSERT INTO legacy_sync_runs VALUES ({marks})", tuple(row))
                    for row in legacy_probes:
                        marks = ", ".join("?" for _ in row)
                        target.execute(f"INSERT INTO legacy_probes VALUES ({marks})", tuple(row))
                scopes_by_run: dict[int, list[tuple]] = {}
                for scope in scopes_all:
                    scopes_by_run.setdefault(int(scope[1]), []).append(scope)
                responses_by_scope: dict[int, list[tuple]] = {}
                for row in response_rows_all:
                    responses_by_scope.setdefault(int(row[2]), []).append(row)
                max_scope_id = max((int(s[0]) for s in scopes_all), default=0)
                next_scope_id = [max_scope_id + 1]
                for run in runs:
                    hook("before_run", run_id=run[0])
                    _reparse_one_run(
                        target,
                        run,
                        scopes_by_run.get(int(run[0]), []),
                        responses_by_scope,
                        archived_by_scope,
                        migrated_observations.get(int(run[0]), []),
                        retime,
                        hook,
                        next_scope_id,
                    )
                with transaction(target):
                    for row in cutover:
                        target.execute(
                            "INSERT INTO cutover (id, completed_at, from_states,"
                            " acceptance_run) VALUES (?,?,?,?)",
                            tuple(row),
                        )
                hook("before_rebuild")
                replayed = rebuild(target)
                hook("after_replay", runs=replayed)
                from .check import check_connection

                failures, _ = check_connection(target)
                if failures:
                    raise RuntimeError(f"--check fails on the reparsed copy: {failures[0]}")
                acceptance = target.execute(
                    "SELECT acceptance_run FROM cutover WHERE id = 1"
                ).fetchone()
                if acceptance is not None and acceptance[0] is not None:
                    accepted_id = int(acceptance[0])
                    new_row = target.execute(
                        "SELECT outcome FROM runs WHERE id = ?", (accepted_id,)
                    ).fetchone()
                    if new_row is not None and new_row[0] != "complete":
                        old = old_outcomes.get(accepted_id, "?")
                        raise RuntimeError(
                            f"acceptance run {accepted_id} is {new_row[0]} (was {old})"
                            " under the new interpretation;"
                            " a new live acceptance run is needed first"
                        )
                hook("before_publish_check")
                if before_publish is not None:
                    before_publish()
                try:
                    os.link(temporary, dest_path)
                except FileExistsError:
                    raise RuntimeError(
                        f"{dest_path} appeared while the reparse was being built;"
                        " the publish aborts and the existing file is left untouched"
                    ) from None
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                target.close()
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        return dest_path
    finally:
        source_lock.release()


def _reparse_one_run(
    target: sqlite3.Connection,
    run: tuple,
    scopes: list[tuple],
    responses_by_scope: dict[int, list[tuple]],
    archived_by_scope: dict[int, list],
    migrated_observations: list[tuple],
    retime: bool,
    hook: CrashHook,
    next_scope_id: list[int] | None = None,
) -> None:
    (
        run_id,
        started_at,
        started_day,
        finished_at,
        finished_day,
        outcome,
        _api_calls,
        code_version,
        item_ids,
        note,
    ) = run
    run_id = int(run_id)
    if retime and isinstance(code_version, str) and not code_version.endswith("+retime"):
        code_version = f"{code_version}+retime"
    if outcome == "migrated":
        with transaction(target):
            target.execute(
                "INSERT INTO runs (id, started_at, started_day, finished_at, finished_day,"
                " outcome, api_calls, code_version, item_ids, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    started_at,
                    started_day,
                    finished_at,
                    finished_day,
                    outcome,
                    _api_calls,
                    code_version,
                    item_ids,
                    note,
                ),
            )
            for scope in scopes:
                target.execute(
                    "INSERT INTO scopes (id, run_id, kind, parent, expected, outcome,"
                    " infer_absence, status, rows, min_date, min_instant, closed_at,"
                    " closed_day, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(scope),
                )
            for row in migrated_observations:
                # New ids: the corrected run may hold more observations than the
                # source, so source ids are not stable across a reparse.
                target.execute(
                    "INSERT INTO observations (run_id, scope_id, response_id, position,"
                    " document_id, parent, sha256, observed_at, observed_day)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]),
                )
        return

    from .check import _recompute_scope

    recomputed: dict[int, dict[str, Any]] = {}
    results: dict[int, Any] = {}
    pages_by_scope: dict[int, list] = {}
    for scope in scopes:
        scope_id = int(scope[0])
        archived = archived_by_scope.get(scope_id)
        if not archived:
            continue
        if retime:
            archived = [
                attempt.__class__(
                    **{**attempt.__dict__, "received_day": local_day(attempt.received_at)}
                )
                for attempt in archived
            ]
        result, close, per_page = _recompute_scope(str(scope[2]), str(scope[3]), archived)
        results[scope_id] = result
        recomputed[scope_id] = close
        pages_by_scope[scope_id] = per_page

    existing_keys = {(str(s[2]), str(s[3])) for s in scopes}
    credit_by_account: dict[str, bool] = {}
    for scope in scopes:
        scope_id = int(scope[0])
        if str(scope[2]) != "accounts" or scope_id not in pages_by_scope:
            continue
        for _, valid, _, _, docs in pages_by_scope[scope_id]:
            if not valid:
                continue
            for document in docs:
                pid = document.get("id")
                if isinstance(pid, str):
                    credit_by_account[pid] = credit_by_account.get(pid, False) or (
                        _is_credit_doc(document)
                    )
    extra_skipped: list[tuple[str, str, str | None, str | None]] = []
    for scope in scopes:
        kind = str(scope[2])
        if kind not in CHILD_KINDS:
            continue
        scope_id = int(scope[0])
        if scope_id not in recomputed or recomputed[scope_id]["outcome"] != "complete":
            continue
        seen_here: dict[str, bool] = {}
        for _, valid, _, _, docs in pages_by_scope.get(scope_id, []):
            if not valid:
                continue
            for document in docs:
                pid = document.get("id")
                if isinstance(pid, str):
                    seen_here[pid] = (
                        credit_by_account.get(pid, False) if kind == "accounts" else True
                    )
        for provider_id, credit in seen_here.items():
            for child in CHILD_KINDS[kind]:
                if child == "bills" and not credit:
                    continue
                if (child, provider_id) not in existing_keys:
                    extra_skipped.append(
                        (
                            child,
                            provider_id,
                            recomputed[scope_id]["closed_at"],
                            recomputed[scope_id]["closed_day"],
                        )
                    )
                    existing_keys.add((child, provider_id))

    ambiguous = _ambiguous_in_memory(scopes, pages_by_scope)
    undone = sum(
        1
        for s in scopes
        if int(s[4]) == 1
        and recomputed.get(int(s[0]), {"outcome": s[5]})["outcome"]
        not in ("complete", "unavailable")
    ) + len(extra_skipped)
    if outcome in ("failed", "interrupted"):
        new_outcome, new_note = outcome, note
    else:
        new_outcome = "complete" if undone == 0 and not ambiguous else "partial"
        new_note = None

    response_inserts: list[tuple] = []
    for scope in scopes:
        scope_id = int(scope[0])
        for row in responses_by_scope.get(scope_id, []):
            received_day = local_day(str(row[12])) if retime else row[13]
            response_inserts.append(
                (
                    row[0],
                    run_id,
                    scope_id,
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    received_day,
                )
            )
    finished_list = [str(r[12]) for r in response_inserts]
    if finished_list:
        new_finished = max(finished_list)
        new_finished_day = local_day(new_finished)
    else:
        new_finished, new_finished_day = finished_at, finished_day
        if retime and new_finished is not None:
            new_finished_day = local_day(str(new_finished))
    new_api_calls = len(response_inserts)

    with transaction(target):
        target.execute(
            "INSERT INTO runs (id, started_at, started_day, finished_at, finished_day,"
            " outcome, api_calls, code_version, item_ids, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                started_at,
                started_day,
                new_finished,
                new_finished_day,
                new_outcome,
                new_api_calls,
                code_version,
                item_ids,
                new_note,
            ),
        )
        for scope in scopes:
            scope_id = int(scope[0])
            kind, parent, expected = str(scope[2]), str(scope[3]), int(scope[4])
            if scope_id in recomputed:
                close = recomputed[scope_id]
                target.execute(
                    "INSERT INTO scopes (id, run_id, kind, parent, expected, outcome,"
                    " infer_absence, status, rows, min_date, min_instant, closed_at,"
                    " closed_day, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope_id,
                        run_id,
                        kind,
                        parent,
                        expected,
                        close["outcome"],
                        close["infer_absence"],
                        close["status"],
                        close["rows"],
                        close["min_date"],
                        close["min_instant"],
                        close["closed_at"],
                        close["closed_day"],
                        close["error"],
                    ),
                )
            else:
                closed_day = scope[12]
                if retime and scope[11] is not None:
                    closed_day = local_day(str(scope[11]))
                target.execute(
                    "INSERT INTO scopes (id, run_id, kind, parent, expected, outcome,"
                    " infer_absence, status, rows, min_date, min_instant, closed_at,"
                    " closed_day, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scope_id,
                        run_id,
                        kind,
                        parent,
                        expected,
                        scope[5],
                        scope[6],
                        scope[7],
                        scope[8],
                        scope[9],
                        scope[10],
                        scope[11],
                        closed_day,
                        scope[13],
                    ),
                )
        for child_kind, child_parent, parent_closed, parent_day in extra_skipped:
            if next_scope_id is None:
                target.execute(
                    "INSERT INTO scopes (run_id, kind, parent, expected, outcome, closed_at,"
                    " closed_day, rows) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        child_kind,
                        child_parent,
                        1,
                        "skipped",
                        parent_closed,
                        parent_day,
                        0,
                    ),
                )
            else:
                fresh = next_scope_id[0]
                next_scope_id[0] += 1
                target.execute(
                    "INSERT INTO scopes (id, run_id, kind, parent, expected, outcome,"
                    " closed_at, closed_day, rows) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        fresh,
                        run_id,
                        child_kind,
                        child_parent,
                        1,
                        "skipped",
                        parent_closed,
                        parent_day,
                        0,
                    ),
                )
        for row in response_inserts:
            target.execute(
                "INSERT INTO responses (id, run_id, scope_id, method, url, status, headers,"
                " body_zlib, body_sha256, raw_bytes, truncated, error, received_at,"
                " received_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
    by_scope_id = {int(s[0]): s for s in scopes}
    ordered = sorted(
        (sid for sid in results),
        key=lambda sid: (
            min(p.attempt_id for p in results[sid].pages) if results[sid].pages else 10**18
        ),
    )
    for scope_id in ordered:
        scope = by_scope_id[scope_id]
        result = results[scope_id]
        view = DeriveScope(scope_id, run_id, str(scope[2]), str(scope[3]), int(scope[4]), "")
        for page in result.pages:
            received = target.execute(
                "SELECT received_at, received_day FROM responses WHERE id = ?",
                (page.attempt_id,),
            ).fetchone()
            assert received is not None
            derive_page(target, view, page, str(received[0]), str(received[1]))
    hook("after_run", run_id=run_id)
