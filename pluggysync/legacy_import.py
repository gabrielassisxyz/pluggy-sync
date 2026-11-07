"""`--candidate <path> --from-states <state> [<state> ...]`: the frozen legacy
databases become the first runs of a candidate ledger.

The legacy collector kept one copy of each record, overwritten on every run,
with a `fetched_at` per row and a `probes` row per walk. Every surviving row
becomes one observation in the run whose window contains its `fetched_at`;
the control tables are copied verbatim; every migrated walk closes
`incomplete` with `infer_absence = 0`, because the legacy run never said
whether it finished, so nothing migrated ever infers an absence. The import
verifies itself against a manifest of the source rows and fails on any
mismatch rather than publish a candidate that disagrees with its inputs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from urllib.parse import parse_qs, urlsplit

from .derive import canonical
from .events import rebuild_projection
from .ledger import local_day, transaction
from .projections import EXTRACTED, PROJECTION_NAMES

LEGACY_TABLES = PROJECTION_NAMES
PARENT_COLUMN = {
    "categories": None,
    "items": "id",
    "accounts": "item_id",
    "investments": "item_id",
    "consents": "item_id",
    "identities": "item_id",
    "transactions": "account_id",
    "bills": "account_id",
    "investment_transactions": "investment_id",
}
LEGACY_ERROR = "legacy: completeness unknown"
# Legacy columns whose extraction was corrected in the projection; parity skips them.
PARITY_SKIP = {"bills": {"closing_date"}}


class ImportFailed(Exception):
    pass


@dataclass(frozen=True)
class Sighting:
    table: str
    provider_id: str
    parent: str
    fetched_at: str
    sha256: str
    body: str
    run_id: int


@dataclass
class ImportReport:
    runs: int = 0
    scopes: int = 0
    sightings: int = 0
    documents: int = 0
    remarks: list[str] = field(default_factory=list)


def _open_readonly(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise ImportFailed(f"state file {path} does not exist")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rows(connection: sqlite3.Connection, sql: str) -> list[tuple]:
    return connection.execute(sql).fetchall()


def _check_prefix(states: list[tuple[str, sqlite3.Connection]]) -> None:
    """Each state's control tables are an exact prefix of the next state's."""
    for (earlier_path, earlier), (later_path, later) in pairwise(states):
        for table, order in (("sync_runs", "id"), ("probes", "rowid")):
            head = _rows(earlier, f"SELECT * FROM {table} ORDER BY {order}")
            full = _rows(later, f"SELECT * FROM {table} ORDER BY {order}")
            if full[: len(head)] != head:
                raise ImportFailed(f"{table} of {earlier_path} is not a prefix of {later_path}'s")


def scope_of_probe(path: str) -> tuple[str, str] | None:
    parts = urlsplit(path)
    query = parse_qs(parts.query)
    if parts.path == "/categories":
        return ("categories", "")
    if parts.path.startswith("/items/"):
        return ("items", parts.path.removeprefix("/items/"))
    if parts.path == "/identity":
        return ("identities", query["itemId"][0])
    if parts.path in ("/investments", "/accounts", "/consents"):
        return (parts.path.lstrip("/"), query["itemId"][0])
    if parts.path == "/v2/transactions":
        return ("transactions", query["accountId"][0])
    if parts.path == "/bills":
        return ("bills", query["accountId"][0])
    return None


def _parent_of(table: str, row: dict) -> str:
    column = PARENT_COLUMN[table]
    if column is None:
        return ""
    value = row[column]
    if not isinstance(value, str) or not value:
        raise ImportFailed(f"{table} row {row['id']!r} has no {column}")
    return value


def _window_of(fetched_at: str, runs: list[tuple]) -> int:
    hits = [
        run_id
        for run_id, started, finished, *_ in runs
        if started <= fetched_at <= (finished or started)
    ]
    if len(hits) != 1:
        raise ImportFailed(
            f"fetched_at {fetched_at} matches {len(hits)} run windows"
            f" ({', '.join(str(h) for h in hits) or 'none'})"
        )
    return hits[0]


def _collect_sightings(
    states: list[tuple[str, sqlite3.Connection]], runs: list[tuple]
) -> list[Sighting]:
    """Distinct sightings across every state; a (table, id) seen in one run by two
    states must agree on parent, fetched_at and body."""
    seen: dict[tuple[str, str, int], Sighting] = {}
    for path, state in states:
        for table in LEGACY_TABLES:
            cursor = state.execute(f"SELECT * FROM {table}")
            names = [column[0] for column in cursor.description]
            for values in cursor.fetchall():
                row = dict(zip(names, values, strict=True))
                try:
                    run_id = _window_of(row["fetched_at"], runs)
                except ImportFailed as failure:
                    raise ImportFailed(f"{table} {row['id']!r} in {path}: {failure}") from None
                sha, body = canonical(json.loads(row["raw"]))
                sighting = Sighting(
                    table, row["id"], _parent_of(table, row), row["fetched_at"], sha, body, run_id
                )
                key = (table, row["id"], run_id)
                earlier = seen.get(key)
                if earlier is not None and earlier != sighting:
                    raise ImportFailed(
                        f"{table} {row['id']!r} differs between states inside run {run_id}"
                        " (parent, fetched_at or body)"
                    )
                seen[key] = sighting
    return list(seen.values())


def _plan_scopes(
    probes: list[tuple], runs: list[tuple], sightings: list[Sighting]
) -> tuple[dict[tuple[int, str, str], dict], list[str]]:
    """Every probed walk, plus the walks the legacy loop ran without probing."""
    finished = {run_id: finished or started for run_id, started, finished, *_ in runs}
    scopes: dict[tuple[int, str, str], dict] = {}
    remarks: list[str] = []
    for run_id, path, status, _, _, fetched_at in probes:
        mapped = scope_of_probe(path)
        if mapped is None:
            raise ImportFailed(f"probe path {path!r} maps to no scope kind")
        kind, parent = mapped
        key = (run_id, kind, parent)
        if key in scopes:
            raise ImportFailed(f"run {run_id} probed {kind}/{parent} twice")
        scopes[key] = {"status": status, "closed_at": fetched_at, "synthetic": False}
    for (run_id, kind, parent), scope in list(scopes.items()):
        if kind == "items" and scope["status"] == 200:
            scopes.setdefault(
                (run_id, "consents", parent),
                {"status": None, "closed_at": finished[run_id], "synthetic": True},
            )
    investments_seen: set[tuple[int, str]] = set()
    for sighting in sightings:
        if sighting.table == "investments":
            investments_seen.add((sighting.run_id, sighting.provider_id))
        if sighting.table == "investment_transactions":
            investments_seen.add((sighting.run_id, sighting.parent))
    for run_id, investment_id in sorted(investments_seen):
        scopes.setdefault(
            (run_id, "investment_transactions", investment_id),
            {"status": None, "closed_at": finished[run_id], "synthetic": True},
        )
    for sighting in sightings:
        if (sighting.run_id, sighting.table, sighting.parent) not in scopes:
            raise ImportFailed(
                f"{sighting.table} {sighting.provider_id!r} (run {sighting.run_id}) has no walk"
                f" to belong to: no probe for {sighting.table}/{sighting.parent}"
            )
    for run_id, *_ in runs:
        if not any(s.run_id == run_id for s in sightings):
            remarks.append(
                f"run {run_id}: no row survived (overwritten by a later run); coverage unknown"
            )
    return scopes, remarks


def _write(
    connection: sqlite3.Connection,
    runs: list[tuple],
    probes: list[tuple],
    scopes: dict[tuple[int, str, str], dict],
    sightings: list[Sighting],
    remarks: list[str],
) -> ImportReport:
    report = ImportReport()
    for seq, probe in enumerate(probes, start=1):
        connection.execute(
            "INSERT INTO legacy_probes (seq, run_id, path, status, rows, note, fetched_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (seq, *probe),
        )
    items_by_run: dict[int, list[str]] = defaultdict(list)
    for run_id, path, *_ in probes:
        mapped = scope_of_probe(path)
        if mapped and mapped[0] == "items":
            items_by_run[run_id].append(mapped[1])
    for run_id, started, finished, api_calls, note in runs:
        connection.execute(
            "INSERT INTO legacy_sync_runs (id, started_at, finished_at, api_calls, note)"
            " VALUES (?,?,?,?,?)",
            (run_id, started, finished, api_calls, note),
        )
        own_remarks = [r for r in remarks if r.startswith(f"run {run_id}:")]
        full_note = "; ".join(
            [part for part in [note, "migrated from the legacy database", *own_remarks] if part]
        )
        connection.execute(
            "INSERT INTO runs (id, started_at, started_day, finished_at, finished_day, outcome,"
            " api_calls, code_version, item_ids, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                started,
                local_day(started),
                finished or started,
                local_day(finished or started),
                "migrated",
                api_calls,
                "unknown",
                json.dumps(items_by_run.get(run_id, [])),
                full_note,
            ),
        )
        report.runs += 1
    rows_per_scope: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    for sighting in sightings:
        rows_per_scope[(sighting.run_id, sighting.table, sighting.parent)].add(sighting.provider_id)
    scope_ids: dict[tuple[int, str, str], int] = {}
    for key in sorted(scopes):
        run_id, kind, parent = key
        scope = scopes[key]
        cursor = connection.execute(
            "INSERT INTO scopes (run_id, kind, parent, expected, outcome, infer_absence, status,"
            " rows, closed_at, closed_day, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                parent,
                1,
                "incomplete",
                0,
                scope["status"],
                len(rows_per_scope.get(key, ())),
                scope["closed_at"],
                local_day(scope["closed_at"]),
                LEGACY_ERROR,
            ),
        )
        scope_ids[key] = int(cursor.lastrowid or 0)
        report.scopes += 1
    for sighting in sorted(
        sightings, key=lambda s: (s.run_id, s.fetched_at, s.table, s.provider_id)
    ):
        connection.execute(
            "INSERT INTO documents (kind, provider_id) VALUES (?,?) ON CONFLICT DO NOTHING",
            (sighting.table, sighting.provider_id),
        )
        document_id = connection.execute(
            "SELECT id FROM documents WHERE kind = ? AND provider_id = ?",
            (sighting.table, sighting.provider_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO versions (sha256, body) VALUES (?,?) ON CONFLICT DO NOTHING",
            (sighting.sha256, sighting.body),
        )
        connection.execute(
            "INSERT INTO observations (run_id, scope_id, response_id, position, document_id,"
            " parent, sha256, observed_at, observed_day) VALUES (?,?,NULL,NULL,?,?,?,?,?)",
            (
                sighting.run_id,
                scope_ids[(sighting.run_id, sighting.table, sighting.parent)],
                document_id,
                sighting.parent,
                sighting.sha256,
                sighting.fetched_at,
                local_day(sighting.fetched_at),
            ),
        )
        report.sightings += 1
    report.documents = int(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
    report.remarks = remarks
    return report


def _verify(
    connection: sqlite3.Connection,
    states: list[tuple[str, sqlite3.Connection]],
    sightings: list[Sighting],
) -> None:
    """The manifest, the verbatim control tables and projection parity with the
    last state. Any mismatch fails the import."""
    last_path, last = states[-1]
    observed = connection.execute(
        "SELECT d.kind, d.provider_id, o.parent, o.observed_at, o.sha256, count(*)"
        " FROM observations o JOIN documents d ON d.id = o.document_id"
        " GROUP BY 1, 2, 3, 4, 5"
    ).fetchall()
    manifest = {(s.table, s.provider_id, s.parent, s.fetched_at, s.sha256) for s in sightings}
    found = {row[:5] for row in observed}
    if any(row[5] != 1 for row in observed) or found != manifest:
        raise ImportFailed(
            f"manifest mismatch: {len(manifest)} sightings in the states,"
            f" {len(found)} distinct observations in the candidate"
        )
    for table, legacy in (("sync_runs", "legacy_sync_runs"), ("probes", "legacy_probes")):
        source = _rows(last, f"SELECT * FROM {table} ORDER BY rowid")
        copied = [
            row[1:] if legacy == "legacy_probes" else row
            for row in _rows(connection, f"SELECT * FROM {legacy} ORDER BY rowid")
        ]
        if source != copied:
            raise ImportFailed(f"{legacy} differs from {table} of {last_path}")
    state_runs = {s.run_id for s in sightings}
    first_run, last_run = min(state_runs), max(state_runs)
    for table in LEGACY_TABLES:
        columns = [
            name for name, _ in EXTRACTED[table] if name not in PARITY_SKIP.get(table, set())
        ]
        cursor = last.execute(f"SELECT id, raw, fetched_at, {', '.join(columns)} FROM {table}")
        expected = {row[0]: row[1:] for row in cursor.fetchall()}
        projected = {
            row[0]: row[1:]
            for row in connection.execute(
                f"SELECT id, raw, fetched_at, {', '.join(columns)}, state, first_seen_run,"
                f" last_seen_run FROM {table}"
            ).fetchall()
        }
        if set(expected) != set(projected):
            raise ImportFailed(
                f"{table}: projection holds {len(projected)} ids, the state {len(expected)}"
            )
        for provider_id, (raw, fetched_at, *values) in expected.items():
            got_raw, got_fetched, *got = projected[provider_id]
            got_values, state, first_seen, last_seen = got[:-3], got[-3], got[-2], got[-1]
            _, canonical_raw = canonical(json.loads(raw))
            if (canonical_raw, fetched_at, list(values)) != (
                got_raw,
                got_fetched,
                list(got_values),
            ):
                raise ImportFailed(f"{table} {provider_id!r}: projection differs from {last_path}")
            runs_of_row = sorted(
                {s.run_id for s in sightings if s.table == table and s.provider_id == provider_id}
            )
            if state != "present" or first_seen != runs_of_row[0] or last_seen != runs_of_row[-1]:
                raise ImportFailed(
                    f"{table} {provider_id!r}: state {state}, seen {first_seen}..{last_seen}"
                )
    union = {(s.table, s.provider_id) for s in sightings}
    if int(connection.execute("SELECT count(*) FROM documents").fetchone()[0]) != len(union):
        raise ImportFailed("document count differs from the union of ids across the states")
    outcomes = [
        row[0] for row in connection.execute("SELECT outcome FROM run_coverage ORDER BY run_id")
    ]
    if (
        any(outcome != "migrated" for outcome in outcomes)
        or len(outcomes) < last_run - first_run + 1
    ):
        raise ImportFailed(f"run_coverage shows {outcomes}, expected every run migrated")


def import_states(connection: sqlite3.Connection, state_paths: list[str]) -> ImportReport:
    """Imports into an empty ledger. The caller owns the file, the lock and the rollback."""
    if not state_paths:
        raise ImportFailed("--from-states needs at least one state file")
    if connection.execute("SELECT count(*) FROM runs").fetchone()[0]:
        raise ImportFailed("the candidate already holds runs; the import needs an empty ledger")
    states = [(path, _open_readonly(path)) for path in state_paths]
    try:
        _check_prefix(states)
        last = states[-1][1]
        runs = _rows(
            last, "SELECT id, started_at, finished_at, api_calls, note FROM sync_runs ORDER BY id"
        )
        probes = _rows(
            last, "SELECT run_id, path, status, rows, note, fetched_at FROM probes ORDER BY rowid"
        )
        if not runs:
            raise ImportFailed(f"{state_paths[-1]} has no sync_runs")
        sightings = _collect_sightings(states, runs)
        scopes, remarks = _plan_scopes(probes, runs, sightings)
        with transaction(connection):
            report = _write(connection, runs, probes, scopes, sightings, remarks)
            rebuild_projection(connection)
            _verify(connection, states, sightings)
        return report
    finally:
        for _, state in states:
            state.close()
