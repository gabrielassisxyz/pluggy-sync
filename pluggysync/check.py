"""`--check`: the integrity report over all three layers, read-only.

Every problem names the defect and the row that carries it. The per-run
cross-links reuse `self_check` from the write path; the rest recomputes what
the archive proves and compares it to what the tables claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zlib
from typing import Any

from .archive import archived_responses
from .derive import canonical, page_is_valid, page_meta
from .ledger import SCHEMA_VERSION, local_day, open_ledger
from .lock import LockHeld, LockRefused, ProcessLock
from .projections import PROJECTION_NAMES
from .replay import replay_walk
from .run import SWEPT, self_check, url_matches_scope
from .walk import FetchResult


def _date_bounds(kind: str, documents: list[dict[str, Any]]) -> tuple[str | None, str | None, bool]:
    from .derive import _date_bounds as derive_bounds

    return derive_bounds(kind, documents)


def _expected_master() -> tuple[dict[tuple[str, str], str | None], int]:
    fd, path = tempfile.mkstemp(prefix=".check-schema-")
    os.close(fd)
    try:
        connection = open_ledger(path)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            rows = connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return {(r[0], r[1]): r[2] for r in rows}, version
        finally:
            connection.close()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _recompute_scope(
    kind: str, parent: str, archived: list[Any]
) -> tuple[FetchResult, dict[str, Any], list[tuple[Any, bool, str | None, str | None, list]]]:
    result = replay_walk(kind, parent, archived)
    invalid: list[str] = []
    documents: list[dict[str, Any]] = []
    per_page: list[tuple[Any, bool, str | None, str | None, list]] = []
    for number, page in enumerate(result.pages, start=1):
        valid, error = page_is_valid(page)
        meta = page_meta(page)
        if valid:
            documents.extend(page.documents)
            per_page.append((page.attempt_id, True, meta, error, list(page.documents)))
        else:
            invalid.append(f"page {number}: {error}")
            per_page.append((page.attempt_id, False, meta, error, []))
    outcome = result.outcome
    if invalid and outcome != "unavailable":
        outcome = "incomplete"
    seen: dict[str, int] = {}
    for document in documents:
        identifier = document.get("id")
        if isinstance(identifier, str):
            seen[identifier] = seen.get(identifier, 0) + 1
    repeated = any(count > 1 for count in seen.values())
    min_date, min_instant, undated = _date_bounds(kind, documents)
    infer = 1 if outcome == "complete" and not repeated and not undated else 0
    if archived:
        closed_at = max(a.received_at for a in archived)
    else:
        closed_at = ""
    errors = [part for part in [result.error, *invalid] if part]
    close = {
        "outcome": outcome,
        "infer_absence": infer,
        "status": result.status or None,
        "rows": len(seen),
        "min_date": min_date,
        "min_instant": min_instant,
        "closed_at": closed_at,
        "closed_day": local_day(closed_at) if closed_at else None,
        "error": "; ".join(errors) if errors else None,
    }
    return result, close, per_page


def _notes_staleness(note: object) -> bool:
    return isinstance(note, str) and "stale " in note


def check_connection(connection: sqlite3.Connection) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    info: list[str] = []

    expected_master, expected_version = _expected_master()
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != expected_version or version != SCHEMA_VERSION:
        problems.append(
            f"user_version {version} does not match the installed schema {expected_version}"
        )
    found = {
        (r[0], r[1]): r[2]
        for r in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    for key, sql in expected_master.items():
        if key not in found:
            problems.append(f"missing {key[0]} {key[1]} (the installed schema needs it)")
        elif found[key] != sql:
            problems.append(f"{key[0]} {key[1]} differs from the installed schema")

    verdict = connection.execute("PRAGMA integrity_check").fetchone()
    if verdict is None or verdict[0] != "ok":
        problems.append(f"integrity_check reports: {verdict[0] if verdict else 'no verdict'}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        problems.append(f"foreign_key_check reports {len(violations)} violations")

    for sha, body in connection.execute("SELECT sha256, body FROM versions"):
        try:
            document = json.loads(body)
        except ValueError as failure:
            problems.append(f"version {sha} is not JSON: {failure}")
            continue
        recomputed, canonical_body = canonical(document)
        if recomputed != sha or canonical_body != body:
            problems.append(f"version {sha} is not the canonical hash of its body")

    for row in connection.execute("SELECT id, body_zlib, body_sha256, raw_bytes FROM responses"):
        response_id, blob, want_sha, want_raw = row
        if blob is None:
            if want_sha is not None or want_raw is not None:
                problems.append(f"response {response_id} has no body but names a hash or size")
            continue
        try:
            raw = zlib.decompress(blob)
        except zlib.error as failure:
            problems.append(f"response {response_id} does not decompress: {failure}")
            continue
        if hashlib.sha256(raw).hexdigest() != want_sha:
            problems.append(f"response {response_id} body does not hash to its body_sha256")
        if len(raw) != want_raw:
            problems.append(
                f"response {response_id} body length {len(raw)} != raw_bytes {want_raw}"
            )

    runs = {r[0]: r[1] for r in connection.execute("SELECT id, outcome FROM runs").fetchall()}
    if any(outcome == "running" for outcome in runs.values()):
        for run_id, outcome in runs.items():
            if outcome == "running":
                problems.append(f"run {run_id} is running")

    scopes = connection.execute(
        "SELECT id, run_id, kind, parent, expected, outcome, infer_absence, status,"
        " rows, min_date, min_instant, closed_at, closed_day, error FROM scopes"
        " ORDER BY id"
    ).fetchall()
    response_counts: dict[int, int] = {}
    for scope_id, count in connection.execute(
        "SELECT scope_id, count(*) FROM responses GROUP BY scope_id"
    ):
        response_counts[int(scope_id)] = int(count)

    for scope in scopes:
        (
            scope_id,
            run_id,
            kind,
            parent,
            _expected,
            outcome,
            infer_absence,
            status,
            rows,
            min_date,
            min_instant,
            closed_at,
            closed_day,
            error,
        ) = scope
        run_outcome = runs.get(run_id, "")
        if run_outcome == "migrated":
            continue
        if response_counts.get(scope_id, 0) == 0:
            continue
        try:
            archived = archived_responses(connection, scope_id)
        except Exception as failure:
            problems.append(f"scope {kind}/{parent} archive does not read back: {failure!r}")
            continue
        try:
            _, close, per_page = _recompute_scope(kind, parent, archived)
        except Exception as failure:
            problems.append(f"scope {kind}/{parent} replay failed: {failure!r}")
            continue
        for field in (
            "outcome",
            "infer_absence",
            "status",
            "rows",
            "min_date",
            "min_instant",
            "closed_at",
            "error",
        ):
            stored = {
                "outcome": outcome,
                "infer_absence": infer_absence,
                "status": status,
                "rows": rows,
                "min_date": min_date,
                "min_instant": min_instant,
                "closed_at": closed_at,
                "closed_day": closed_day,
                # A scope the recovery sweep closed carries the `swept: ` marker the
                # live walk never writes; the replay cannot know a sweep happened.
                "error": error.removeprefix(SWEPT) if isinstance(error, str) else error,
            }[field]
            if close[field] != stored:
                problems.append(
                    f"scope {kind}/{parent} {field} {stored!r} != recomputed {close[field]!r}"
                )
        stored_derivations = {
            r[0]: (r[1], r[2], r[3])
            for r in connection.execute(
                "SELECT p.response_id, p.valid, p.page_meta, p.error FROM page_derivations p"
                " JOIN responses r ON r.id = p.response_id WHERE r.scope_id = ?",
                (scope_id,),
            )
        }
        wanted_ids = {attempt for attempt, _, _, _, _ in per_page}
        if set(stored_derivations) != wanted_ids:
            problems.append(
                f"scope {kind}/{parent} page_derivations cover {sorted(stored_derivations)}"
                f" != pages {sorted(wanted_ids)}"
            )
        for attempt_id, valid, meta, page_error, docs in per_page:
            stored_row = stored_derivations.get(attempt_id)
            if stored_row is None:
                continue
            if (
                stored_row[0] != (1 if valid else 0)
                or stored_row[1] != meta
                or (stored_row[2] or None) != (page_error or None)
            ):
                problems.append(
                    f"scope {kind}/{parent} page_derivation for response {attempt_id} != recomputed"
                )
            stored_obs = connection.execute(
                "SELECT v.body FROM observations o JOIN versions v ON v.sha256 = o.sha256"
                " WHERE o.scope_id = ? AND o.response_id = ? ORDER BY o.position",
                (scope_id, attempt_id),
            ).fetchall()
            if not valid:
                if stored_obs:
                    problems.append(
                        f"scope {kind}/{parent} invalid page {attempt_id} has"
                        f" {len(stored_obs)} observations"
                    )
                continue
            if len(stored_obs) != len(docs):
                problems.append(
                    f"scope {kind}/{parent} page {attempt_id} holds {len(stored_obs)}"
                    f" observations != {len(docs)} documents re-parsed"
                )
                continue
            for position, document in enumerate(docs):
                want_sha, _ = canonical(document)
                got_body = stored_obs[position][0]
                try:
                    got_sha, _ = canonical(json.loads(got_body))
                except ValueError:
                    problems.append(
                        f"scope {kind}/{parent} page {attempt_id} observation"
                        f" {position} is not JSON"
                    )
                    break
                if got_sha != want_sha:
                    problems.append(
                        f"scope {kind}/{parent} page {attempt_id} observation"
                        f" {position} != re-parsed document"
                    )
                    break

    for run_id, outcome in runs.items():
        if outcome == "running":
            continue
        pending = int(
            connection.execute(
                "SELECT count(*) FROM scopes WHERE run_id = ? AND outcome = 'pending'",
                (run_id,),
            ).fetchone()[0]
        )
        if pending:
            problems.append(f"run {run_id} is terminal with {pending} pending scopes")
        if outcome in ("failed", "interrupted"):
            row = connection.execute(
                "SELECT finished_at, note FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not row[0]:
                problems.append(f"run {run_id} {outcome} has no finished_at")
            if not row[1]:
                problems.append(f"run {run_id} {outcome} has no note")
            continue
        if outcome == "migrated":
            continue
        undone = int(
            connection.execute(
                "SELECT count(*) FROM scopes WHERE run_id = ? AND expected = 1"
                " AND outcome NOT IN ('complete', 'unavailable')",
                (run_id,),
            ).fetchone()[0]
        )
        ambiguous = int(
            connection.execute(
                "SELECT count(*) FROM ambiguities WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        want = "complete" if undone == 0 and ambiguous == 0 else "partial"
        note = connection.execute("SELECT note FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        # A run the freshness check closed partial recorded why; the walks alone
        # would read complete, and the check cannot re-judge freshness under
        # today's thresholds without inventing a disagreement.
        stale_partial = outcome == "partial" and want == "complete" and _notes_staleness(note)
        if outcome != want and not stale_partial:
            problems.append(
                f"run {run_id} outcome {outcome!r} != recomputed {want!r}"
                f" ({undone} owed scopes unfinished, {ambiguous} ambiguous documents)"
            )
        api_calls = int(
            connection.execute(
                "SELECT count(*) FROM responses WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        stored_calls = connection.execute(
            "SELECT api_calls FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        if stored_calls != api_calls:
            problems.append(
                f"run {run_id} api_calls {stored_calls} != {api_calls} archived attempts"
            )

    for run_id in runs:
        try:
            found_problems = self_check(connection, run_id)
        except sqlite3.Error as failure:
            problems.append(f"run {run_id}: self-check cannot run: {failure}")
            continue
        for found in found_problems:
            problems.append(f"run {run_id}: {found}")
        urls = connection.execute(
            "SELECT r.url, s.kind, s.parent FROM responses r JOIN scopes s ON s.id = r.scope_id"
            " WHERE r.run_id = ?",
            (run_id,),
        ).fetchall()
        for url, kind, parent in urls:
            if not url_matches_scope(kind, parent, url):
                problems.append(
                    f"run {run_id}: archived URL {url} does not match scope {kind}/{parent}"
                )
                break

    live_runs = {rid for rid, outcome in runs.items() if outcome != "migrated"}
    for (run_id,) in connection.execute(
        "SELECT DISTINCT run_id FROM observations WHERE response_id IS NULL"
    ):
        if run_id in live_runs:
            problems.append(f"run {run_id}: live observations without a response")
    for (run_id,) in connection.execute(
        "SELECT DISTINCT run_id FROM observations WHERE response_id IS NOT NULL"
    ):
        if runs.get(run_id) == "migrated":
            problems.append(f"run {run_id}: migrated observations with a response")

    orphans = connection.execute(
        "SELECT count(*) FROM documents d LEFT JOIN observations o"
        " ON o.document_id = d.id WHERE o.id IS NULL"
    ).fetchone()[0]
    if orphans:
        problems.append(f"{orphans} documents have no observation")

    problems.extend(_layer3_problems(connection))
    problems.extend(_moved_problems(connection, info))
    return problems, info


def _layer3_problems(connection: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    fd, path = tempfile.mkstemp(prefix=".check-layer3-")
    os.close(fd)
    try:
        os.chmod(path, 0o600)
        target = sqlite3.connect(path)
        try:
            connection.backup(target)
        finally:
            target.close()
        working = open_ledger(path)
        try:
            from .events import rebuild_projection

            with working:
                pass
            from .ledger import transaction

            with transaction(working):
                rebuild_projection(working)
            names = (*PROJECTION_NAMES, "events", "ambiguities", "document_state")
            for name in names:
                order = "ORDER BY rowid" if name != "projection_state" else ""
                before = sorted(connection.execute(f"SELECT * FROM {name} {order}").fetchall())
                after = sorted(working.execute(f"SELECT * FROM {name} {order}").fetchall())
                if before != after:
                    problems.append(
                        f"layer 3 table {name} differs from a rebuild"
                        f" ({len(before)} rows != {len(after)} rows)"
                    )
            before_state = connection.execute("SELECT * FROM projection_state").fetchall()
            after_state = working.execute("SELECT * FROM projection_state").fetchall()
            if sorted(before_state) != sorted(after_state):
                problems.append("projection_state checkpoint differs from a rebuild")
        finally:
            working.close()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return problems


def _moved_problems(connection: sqlite3.Connection, info: list[str]) -> list[str]:
    problems: list[str] = []
    rows = connection.execute(
        "SELECT o.document_id, d.provider_id, o.run_id, o.parent FROM observations o"
        " JOIN documents d ON d.id = o.document_id"
        " ORDER BY o.document_id, o.run_id, o.id"
    ).fetchall()
    per_document: dict[int, list[tuple[int, str]]] = {}
    names: dict[int, str] = {}
    ambiguous = {
        (int(r), int(d))
        for r, d in connection.execute("SELECT run_id, document_id FROM ambiguities")
    }
    for document_id, provider_id, run_id, parent in rows:
        names[int(document_id)] = str(provider_id)
        per_document.setdefault(int(document_id), []).append((int(run_id), str(parent)))
    for document_id, sightings in per_document.items():
        by_run: dict[int, list[str]] = {}
        for run_id, parent in sightings:
            by_run.setdefault(run_id, [])
            if parent not in by_run[run_id]:
                by_run[run_id].append(parent)
        previous: str | None = None
        for run_id in sorted(by_run):
            parents = by_run[run_id]
            if len(parents) != 1 or (run_id, document_id) in ambiguous:
                continue
            parent = parents[0]
            if previous is not None and parent != previous:
                has = connection.execute(
                    "SELECT 1 FROM events WHERE run_id = ? AND document_id = ? AND event = 'moved'",
                    (run_id, document_id),
                ).fetchone()
                if has is None:
                    problems.append(
                        f"document {names[document_id]} changed parent from"
                        f" {previous} to {parent} in run {run_id} without a moved event"
                    )
                else:
                    info.append(
                        f"document {names[document_id]} moved from {previous}"
                        f" to {parent} in run {run_id}"
                    )
            previous = parent
    return problems


def check_file(path: str) -> tuple[int, list[str], list[str]]:
    """Read-only check of the file at `path`. Returns (exit code, problems, info).

    Takes the shared lock first: a held writer lock reports "writer active"
    without reading a byte.
    """
    if not os.path.exists(path):
        return 1, [f"{path} does not exist"], []
    try:
        lock = ProcessLock.shared(path)
    except LockHeld:
        return 1, [f"writer active: another process holds {path}"], []
    except (OSError, LockRefused) as failure:
        return 1, [f"cannot lock {path}: {failure}"], []
    try:
        if (os.stat(path).st_mode & 0o777) != 0o600:
            mode = oct(os.stat(path).st_mode & 0o777)
            return 1, [f"file mode {mode} is not 0600"], []
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as failure:
            return 1, [f"cannot open {path} read-only: {failure}"], []
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            problems, info = check_connection(connection)
        finally:
            connection.close()
        return (0 if not problems else 1), problems, info
    finally:
        lock.release()
