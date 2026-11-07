"""Observation fixtures written straight into a ledger, no HTTP: runs, closed
scopes and sightings, then `project_run` over them."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from pluggysync.derive import canonical
from pluggysync.events import project_run
from pluggysync.ledger import local_day, open_ledger, transaction

NOON = "2026-01-01T12:00:00+00:00"


class HandBuilt:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "ledger.db")
        self.db: sqlite3.Connection = open_ledger(self.path)
        self._clock = 0

    def close(self) -> None:
        self.db.close()
        self.directory.cleanup()

    def tick(self, hours: int = 24) -> str:
        """A later instant each call: runs are a day apart by default."""
        self._clock += hours
        moment = datetime(2026, 1, 1, 12, tzinfo=UTC) + timedelta(hours=self._clock)
        return moment.isoformat()

    # -- rows -----------------------------------------------------------------

    def run(self, outcome: str = "complete", at: str | None = None) -> int:
        at = at or self.tick()
        with transaction(self.db):
            cursor = self.db.execute(
                "INSERT INTO runs (started_at, started_day, finished_at, finished_day, outcome,"
                " code_version, item_ids) VALUES (?,?,?,?,?,?,?)",
                (at, local_day(at), at, local_day(at), outcome, "test", "[]"),
            )
        return int(cursor.lastrowid or 0)

    def scope(
        self,
        run_id: int,
        kind: str,
        parent: str,
        outcome: str = "complete",
        infer_absence: int | None = None,
        min_date: str | None = None,
        rows: int = 0,
        min_instant: str | None = None,
    ) -> int:
        at = self.db.execute("SELECT finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        if infer_absence is None:
            infer_absence = 1 if outcome == "complete" else 0
        with transaction(self.db):
            cursor = self.db.execute(
                "INSERT INTO scopes (run_id, kind, parent, expected, outcome, infer_absence,"
                " status, rows, min_date, min_instant, closed_at, closed_day)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    kind,
                    parent,
                    1,
                    outcome,
                    infer_absence,
                    200,
                    rows,
                    min_date,
                    min_instant,
                    at,
                    local_day(at),
                ),
            )
        return int(cursor.lastrowid or 0)

    def sight(
        self,
        run_id: int,
        scope_id: int,
        body: dict[str, Any],
        parent: str | None = None,
        at: str | None = None,
    ) -> int:
        kind, scope_parent, closed_at = self.db.execute(
            "SELECT kind, parent, closed_at FROM scopes WHERE id = ?", (scope_id,)
        ).fetchone()
        parent = scope_parent if parent is None else parent
        at = at or closed_at
        sha, canonical_body = canonical(body)
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO documents (kind, provider_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                (kind, body["id"]),
            )
            document_id = self.db.execute(
                "SELECT id FROM documents WHERE kind = ? AND provider_id = ?", (kind, body["id"])
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO versions (sha256, body) VALUES (?,?) ON CONFLICT DO NOTHING",
                (sha, canonical_body),
            )
            cursor = self.db.execute(
                "INSERT INTO observations (run_id, scope_id, response_id, position, document_id,"
                " parent, sha256, observed_at, observed_day) VALUES (?,?,NULL,NULL,?,?,?,?,?)",
                (run_id, scope_id, document_id, parent, sha, at, local_day(at)),
            )
        return int(cursor.lastrowid or 0)

    def project(self, run_id: int) -> None:
        with transaction(self.db):
            project_run(self.db, run_id)

    # -- readers --------------------------------------------------------------

    def events(self, run_id: int | None = None) -> list[tuple[Any, ...]]:
        sql = (
            "SELECT e.run_id, d.provider_id, e.seq, e.event, e.old_sha256, e.new_sha256,"
            " e.old_parent, e.new_parent, e.effective_at, e.effective_day FROM events e"
            " JOIN documents d ON d.id = e.document_id"
        )
        if run_id is not None:
            sql += f" WHERE e.run_id = {int(run_id)}"
        return self.db.execute(sql + " ORDER BY e.run_id, d.kind, d.provider_id, e.seq").fetchall()

    def labels(self, run_id: int, provider_id: str) -> list[str]:
        return [
            event
            for run, pid, seq, event, *_ in self.events(run_id)
            if pid == provider_id and run == run_id
        ]

    def state(self, kind: str, provider_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT ds.state, ds.first_seen_run, ds.last_seen_run, ds.state_run, ds.ambiguous_run,"
            " ds.parent, ds.sha256 FROM document_state ds JOIN documents d ON d.id = ds.document_id"
            " WHERE d.kind = ? AND d.provider_id = ?",
            (kind, provider_id),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "state",
            "first_seen_run",
            "last_seen_run",
            "state_run",
            "ambiguous_run",
            "parent",
            "sha256",
        )
        return dict(zip(keys, row, strict=True))

    def layer3(self) -> tuple[list[tuple[Any, ...]], ...]:
        return (
            self.db.execute("SELECT * FROM events ORDER BY run_id, document_id, seq").fetchall(),
            self.db.execute("SELECT * FROM ambiguities ORDER BY run_id, document_id").fetchall(),
            self.db.execute("SELECT * FROM document_state ORDER BY document_id").fetchall(),
            self.db.execute("SELECT * FROM projection_state").fetchall(),
        )
