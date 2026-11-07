"""Layer 3: what changed, derived once per run from the sightings.

Events are computed after every scope of the run is derived, so the result does
not depend on walk order, and they are replayable: `rebuild_projection` clears
layer 3 and projects every run again, and must land on the same rows. State per
document lives in `document_state`; the per-kind current-state tables are
projected from it.

Disappearance is an inference and is drawn only where the evidence allows it:
from a walk that was complete with no repeated id, over the date range it
actually returned, for a document whose parent that walk covered.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .dates import DATE_FIELDS, business_date

PARENT_OF = {
    "transactions": "accounts",
    "bills": "accounts",
    "investment_transactions": "investments",
}
CHILDREN_OF = {"accounts": ("transactions", "bills"), "investments": ("investment_transactions",)}
GONE = ("removed", "parent_gone")


class ProjectionBlocked(Exception):
    """A terminal run failed its self-check; the checkpoint does not advance over it."""

    def __init__(self, run_id: int, problems: list[str]) -> None:
        super().__init__(f"run {run_id} failed its self-check: {'; '.join(problems)}")
        self.run_id = run_id
        self.problems = problems


@dataclass(frozen=True)
class Sighting:
    observation_id: int
    parent: str
    sha256: str
    observed_at: str
    observed_day: str


@dataclass(frozen=True)
class State:
    state: str
    first_seen_run: int
    ambiguous_run: int | None
    parent: str
    sha256: str


def ambiguous_documents(
    connection: sqlite3.Connection, run_id: int
) -> dict[int, tuple[list[str], str]]:
    """Documents seen in the run with two bodies or under two parents."""
    rows = connection.execute(
        "SELECT document_id, count(DISTINCT sha256), count(DISTINCT parent),"
        " json_group_array(DISTINCT parent) FROM observations WHERE run_id = ?"
        " GROUP BY document_id HAVING count(DISTINCT sha256) > 1 OR count(DISTINCT parent) > 1",
        (run_id,),
    ).fetchall()
    ambiguous: dict[int, tuple[list[str], str]] = {}
    for document_id, bodies, parents, parent_list in rows:
        reasons = []
        if bodies > 1:
            reasons.append("two bodies in one run")
        if parents > 1:
            reasons.append("two parents in one run")
        ambiguous[int(document_id)] = (sorted(json.loads(parent_list)), "; ".join(reasons))
    return ambiguous


class _Projector:
    def __init__(self, connection: sqlite3.Connection, run_id: int) -> None:
        self.db = connection
        self.run = run_id
        self._seq: dict[int, int] = {}
        self.ambiguous = ambiguous_documents(connection, run_id)

    # -- reads ----------------------------------------------------------------

    def _state(self, document_id: int) -> State | None:
        row = self.db.execute(
            "SELECT state, first_seen_run, ambiguous_run, parent, sha256 FROM document_state"
            " WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return State(*row) if row else None

    def _sightings_this_run(self) -> dict[int, list[Sighting]]:
        rows = self.db.execute(
            "SELECT document_id, id, parent, sha256, observed_at, observed_day FROM observations"
            " WHERE run_id = ? ORDER BY document_id, id",
            (self.run,),
        ).fetchall()
        per_document: dict[int, list[Sighting]] = {}
        for document_id, *fields in rows:
            per_document.setdefault(int(document_id), []).append(Sighting(*fields))
        return per_document

    def _baseline(self, document_id: int) -> Sighting | None:
        """The latest earlier unambiguous observation; an ambiguous run is never a baseline."""
        row = self.db.execute(
            "SELECT o.id, o.parent, o.sha256, o.observed_at, o.observed_day FROM observations o"
            " WHERE o.document_id = ? AND o.run_id < ? AND NOT EXISTS ("
            "   SELECT 1 FROM ambiguities a"
            "   WHERE a.run_id = o.run_id AND a.document_id = o.document_id)"
            " ORDER BY o.run_id DESC, o.id DESC LIMIT 1",
            (document_id, self.run),
        ).fetchone()
        return Sighting(*row) if row else None

    def _kind_of(self, document_id: int) -> str:
        return str(
            self.db.execute("SELECT kind FROM documents WHERE id = ?", (document_id,)).fetchone()[0]
        )

    def _body_date(self, kind: str, sha256: str) -> str | None:
        body = self.db.execute("SELECT body FROM versions WHERE sha256 = ?", (sha256,)).fetchone()[
            0
        ]
        return business_date(kind, json.loads(body))

    # -- writes ---------------------------------------------------------------

    def _emit(
        self,
        document_id: int,
        event: str,
        old: tuple[str | None, str | None],
        new: tuple[str | None, str | None],
        effective_at: str,
        effective_day: str,
    ) -> None:
        seq = self._seq.get(document_id, 0) + 1
        self._seq[document_id] = seq
        self.db.execute(
            "INSERT INTO events (run_id, document_id, seq, event, old_sha256, new_sha256,"
            " old_parent, new_parent, effective_at, effective_day) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self.run,
                document_id,
                seq,
                event,
                old[0],
                new[0],
                old[1],
                new[1],
                effective_at,
                effective_day,
            ),
        )

    def _set_present(self, document_id: int, sighting: Sighting, ambiguous: bool) -> None:
        prior = self._state(document_id)
        first_seen = prior.first_seen_run if prior else self.run
        self.db.execute(
            "INSERT OR REPLACE INTO document_state (document_id, state, first_seen_run,"
            " last_seen_run, state_run, ambiguous_run, parent, sha256, observed_at, observed_day)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                "present",
                first_seen,
                self.run,
                self.run,
                self.run if ambiguous else None,
                sighting.parent,
                sighting.sha256,
                sighting.observed_at,
                sighting.observed_day,
            ),
        )

    def _set_gone(self, document_id: int, state: str) -> None:
        self.db.execute(
            "UPDATE document_state SET state = ?, state_run = ?, ambiguous_run = NULL"
            " WHERE document_id = ?",
            (state, self.run, document_id),
        )

    # -- the three passes -----------------------------------------------------

    def record_ambiguities(self) -> None:
        for document_id, (parents, reason) in self.ambiguous.items():
            self.db.execute(
                "INSERT OR IGNORE INTO ambiguities (run_id, document_id, candidate_parents, reason)"
                " VALUES (?,?,?,?)",
                (self.run, document_id, json.dumps(parents), reason),
            )

    def sightings(self) -> set[int]:
        observed = self._sightings_this_run()
        for document_id in sorted(observed):
            sightings = observed[document_id]
            if document_id in self.ambiguous:
                # The display sighting does not depend on walk order.
                display = min(sightings, key=lambda s: (s.parent, s.sha256, -s.observation_id))
                self._set_present(document_id, display, ambiguous=True)
                continue
            last = sightings[-1]
            prior = self._state(document_id)
            baseline = self._baseline(document_id)
            new = (last.sha256, last.parent)
            when = (last.observed_at, last.observed_day)
            if baseline is None:
                self._emit(document_id, "appeared", (None, None), new, *when)
            else:
                old = (baseline.sha256, baseline.parent)
                moved = baseline.parent != last.parent
                if prior is not None and prior.state in GONE:
                    if moved:
                        self._emit(document_id, "moved", old, new, *when)
                    self._emit(document_id, "reappeared", old, new, *when)
                elif moved:
                    self._emit(document_id, "moved", old, new, *when)
                    if baseline.sha256 != last.sha256:
                        self._emit(document_id, "changed", old, new, *when)
                elif baseline.sha256 != last.sha256:
                    self._emit(document_id, "changed", old, new, *when)
            self._set_present(document_id, last, ambiguous=False)
        return set(observed)

    def disappearance(self, observed: set[int]) -> dict[int, tuple[str, str]]:
        """Returns the documents removed in this run with their effective time."""
        safe = self.db.execute(
            "SELECT kind, parent, min_date, closed_at, closed_day FROM scopes"
            " WHERE run_id = ? AND infer_absence = 1 ORDER BY id",
            (self.run,),
        ).fetchall()
        safe_by_key = {
            (kind, parent): (min_date, at, day) for kind, parent, min_date, at, day in safe
        }
        removed: dict[int, tuple[str, str]] = {}
        for kind, parent, min_date, closed_at, closed_day in safe:
            for document_id, sha in self.db.execute(
                "SELECT ds.document_id, ds.sha256 FROM document_state ds"
                " JOIN documents d ON d.id = ds.document_id"
                " WHERE d.kind = ? AND ds.parent = ? AND ds.state = 'present'"
                " AND ds.ambiguous_run IS NULL ORDER BY ds.document_id",
                (kind, parent),
            ).fetchall():
                if document_id in observed or document_id in removed:
                    continue
                if not self._in_range(kind, sha, min_date):
                    continue
                removed[document_id] = (closed_at, closed_day)
        for document_id, candidates in self._ambiguous_candidates(observed, safe_by_key):
            removed[document_id] = candidates
        for document_id in sorted(removed):
            state = self._state(document_id)
            assert state is not None
            at, day = removed[document_id]
            self._emit(document_id, "removed", (state.sha256, state.parent), (None, None), at, day)
            self._set_gone(document_id, "removed")
        return removed

    def _in_range(self, kind: str, sha: str, min_date: str | None) -> bool:
        if kind not in DATE_FIELDS:
            return True
        if min_date is None:
            return False
        date = self._body_date(kind, sha)
        return date is not None and date >= min_date

    def _ambiguous_candidates(
        self, observed: set[int], safe_by_key: dict[tuple[str, str], tuple[str | None, str, str]]
    ) -> list[tuple[int, tuple[str, str]]]:
        """An ambiguous document is removed only when every candidate parent was
        walked absence-safe in this run over the dates it was sighted under."""
        found: list[tuple[int, tuple[str, str]]] = []
        rows = self.db.execute(
            "SELECT ds.document_id, d.kind, ds.ambiguous_run, a.candidate_parents"
            " FROM document_state ds"
            " JOIN documents d ON d.id = ds.document_id"
            " JOIN ambiguities a ON a.run_id = ds.ambiguous_run AND a.document_id = ds.document_id"
            " WHERE ds.state = 'present' AND ds.ambiguous_run IS NOT NULL ORDER BY ds.document_id",
        ).fetchall()
        for document_id, kind, ambiguous_run, candidate_parents in rows:
            if document_id in observed:
                continue
            latest: tuple[str, str] | None = None
            for parent in json.loads(candidate_parents):
                scope = safe_by_key.get((kind, parent))
                if scope is None:
                    latest = None
                    break
                min_date, at, day = scope
                sighted = [
                    sha
                    for (sha,) in self.db.execute(
                        "SELECT DISTINCT sha256 FROM observations"
                        " WHERE run_id = ? AND document_id = ? AND parent = ?",
                        (ambiguous_run, document_id, parent),
                    )
                ]
                if not all(self._in_range(kind, sha, min_date) for sha in sighted):
                    latest = None
                    break
                if latest is None or at > latest[0]:
                    latest = (at, day)
            if latest is not None:
                found.append((document_id, latest))
        return found

    def propagation(self, observed: set[int], removed: dict[int, tuple[str, str]]) -> None:
        for parent_id in sorted(removed):
            kind = self._kind_of(parent_id)
            if kind not in CHILDREN_OF:
                continue
            provider_id = self.db.execute(
                "SELECT provider_id FROM documents WHERE id = ?", (parent_id,)
            ).fetchone()[0]
            at, day = removed[parent_id]
            marks = ",".join("?" for _ in CHILDREN_OF[kind])
            for child_id, ambiguous_run in self.db.execute(
                "SELECT ds.document_id, ds.ambiguous_run FROM document_state ds"
                " JOIN documents d ON d.id = ds.document_id"
                f" WHERE d.kind IN ({marks}) AND ds.state = 'present'"
                " AND (ds.parent = ? OR ds.ambiguous_run IS NOT NULL) ORDER BY ds.document_id",
                (*CHILDREN_OF[kind], provider_id),
            ).fetchall():
                if child_id in observed:
                    continue
                if ambiguous_run is not None and not self._every_candidate_gone(
                    child_id, ambiguous_run, kind
                ):
                    continue
                state = self._state(child_id)
                assert state is not None
                self._emit(
                    child_id, "parent_gone", (state.sha256, state.parent), (None, None), at, day
                )
                self._set_gone(child_id, "parent_gone")

    def _every_candidate_gone(self, child_id: int, ambiguous_run: int, parent_kind: str) -> bool:
        row = self.db.execute(
            "SELECT candidate_parents FROM ambiguities WHERE run_id = ? AND document_id = ?",
            (ambiguous_run, child_id),
        ).fetchone()
        for parent in json.loads(row[0]):
            state = self.db.execute(
                "SELECT ds.state FROM document_state ds JOIN documents d ON d.id = ds.document_id"
                " WHERE d.kind = ? AND d.provider_id = ?",
                (parent_kind, parent),
            ).fetchone()
            if state is None or state[0] not in GONE:
                return False
        return True

    def checkpoint(self) -> None:
        version = self.db.execute(
            "SELECT code_version FROM runs WHERE id = ?", (self.run,)
        ).fetchone()[0]
        self.db.execute(
            "INSERT OR REPLACE INTO projection_state (id, projected_run, code_version)"
            " VALUES (1, ?, ?)",
            (self.run, version),
        )


def project_run(connection: sqlite3.Connection, run_id: int) -> dict[int, tuple[list[str], str]]:
    """Ambiguities, sighting events, disappearance, propagation, checkpoint, for one
    run whose scopes are all derived. Runs inside the caller's transaction."""
    projector = _Projector(connection, run_id)
    projector.record_ambiguities()
    observed = projector.sightings()
    removed = projector.disappearance(observed)
    projector.propagation(observed, removed)
    projector.checkpoint()
    return projector.ambiguous


def project_pending_runs(
    connection: sqlite3.Connection, check: Callable[[sqlite3.Connection, int], list[str]]
) -> list[int]:
    """Projects every terminal run after the checkpoint, in id order. A run that
    fails its self-check stops the advance: the checkpoint never moves over bad data."""
    row = connection.execute("SELECT projected_run FROM projection_state WHERE id = 1").fetchone()
    checkpoint = int(row[0]) if row else 0
    pending = [
        int(run_id)
        for (run_id,) in connection.execute(
            "SELECT id FROM runs WHERE id > ? AND outcome <> 'running' ORDER BY id", (checkpoint,)
        )
    ]
    projected: list[int] = []
    for run_id in pending:
        problems = check(connection, run_id)
        if problems:
            raise ProjectionBlocked(run_id, problems)
        project_run(connection, run_id)
        projected.append(run_id)
    return projected


def rebuild_projection(connection: sqlite3.Connection) -> list[int]:
    """Clears layer 3 and projects every terminal run again, in id order."""
    for table in ("events", "ambiguities", "document_state", "projection_state"):
        connection.execute(f"DELETE FROM {table}")
    runs = [
        int(run_id)
        for (run_id,) in connection.execute(
            "SELECT id FROM runs WHERE outcome <> 'running' ORDER BY id"
        )
    ]
    for run_id in runs:
        project_run(connection, run_id)
    return runs
