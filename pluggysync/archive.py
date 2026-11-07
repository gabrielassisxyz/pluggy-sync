"""Layer 1: every HTTP attempt, committed the moment it arrives.

The callback runs inside the walk, before the page is parsed and before any
retry sleep, in a transaction of its own, so a crash one instant later has
already lost nothing and no write lock is held across an HTTP wait. The API
key travels in a request header and request headers are never stored; `/auth`
never reaches this callback at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .ledger import local_day, transaction
from .transport import PageAttempt

CrashHook = Callable[..., None]


@dataclass(frozen=True)
class ArchivedResponse:
    """One `responses` row, as the replay and the derivation read it back."""

    id: int
    url: str
    status: int
    headers: list[list[str]] | None
    body: bytes | None
    error: str | None
    received_at: str
    received_day: str
    truncated: bool


def response_columns(attempt: PageAttempt) -> dict[str, Any]:
    """The row for one attempt. The body columns are all present or all absent,
    and an empty `b''` body is a body: 401 and 404 often come with one."""
    body = attempt.body_bytes
    archived: dict[str, Any] = {
        "method": attempt.method,
        "url": attempt.url,
        "status": attempt.status,
        "headers": json.dumps(attempt.headers) if attempt.headers or attempt.status > 0 else None,
        "body_zlib": zlib.compress(body) if body is not None else None,
        "body_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
        "raw_bytes": len(body) if body is not None else None,
        "truncated": 1 if attempt.error == "truncated" else 0,
        "error": attempt.error,
        "received_at": attempt.received_at,
        "received_day": local_day(attempt.received_at),
    }
    return archived


class Archive:
    """The `on_attempt` callback of one run. `scope_id` is set before each walk."""

    def __init__(
        self, connection: sqlite3.Connection, run_id: int, crash_at: CrashHook | None = None
    ) -> None:
        self._connection = connection
        self._run_id = run_id
        self._crash_at = crash_at
        self.scope_id: int | None = None
        self.failure: Exception | None = None

    def __call__(self, attempt: PageAttempt) -> int:
        if self.scope_id is None:
            raise RuntimeError("archive callback used outside a scope")
        row = {"run_id": self._run_id, "scope_id": self.scope_id, **response_columns(attempt)}
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        try:
            with transaction(self._connection):
                cursor = self._connection.execute(
                    f"INSERT INTO responses ({names}) VALUES ({marks})", list(row.values())
                )
                response_id = int(cursor.lastrowid or 0)
            if self._crash_at is not None:
                self._crash_at("after_archive_commit", response_id=response_id, url=attempt.url)
        except Exception as failure:
            self.failure = failure
            raise
        return response_id


def archived_responses(connection: sqlite3.Connection, scope_id: int) -> list[ArchivedResponse]:
    rows = connection.execute(
        "SELECT id, url, status, headers, body_zlib, error, received_at, received_day, truncated"
        " FROM responses WHERE scope_id = ? ORDER BY id",
        (scope_id,),
    ).fetchall()
    return [
        ArchivedResponse(
            id=row[0],
            url=row[1],
            status=row[2],
            headers=json.loads(row[3]) if row[3] is not None else None,
            body=zlib.decompress(row[4]) if row[4] is not None else None,
            error=row[5],
            received_at=row[6],
            received_day=row[7],
            truncated=bool(row[8]),
        )
        for row in rows
    ]
