"""Layer 2: pages become documents, versions and observations; a scope closes
by the evidence its pages hold.

One transaction per page, so page 1's documents survive a malformed page 2 and
a crash between pages loses nothing already derived. Derivation is idempotent:
a page with a `page_derivations` row is never derived again, and the recovery
sweep relies on that to finish what a crash interrupted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .dates import DATE_FIELDS, business_date, transaction_instant
from .ledger import local_day, transaction
from .walk import FetchResult, Page


@dataclass(frozen=True)
class Scope:
    id: int
    run_id: int
    kind: str
    parent: str
    expected: int
    outcome: str


@dataclass(frozen=True)
class ScopeClose:
    outcome: str
    infer_absence: int
    status: int | None
    rows: int
    min_date: str | None
    min_instant: str | None
    closed_at: str
    closed_day: str
    error: str | None


def canonical(document: dict[str, Any]) -> tuple[str, str]:
    """Canonical JSON and its SHA-256: the identity of a document version."""
    body = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest(), body


def id_error(documents: list[dict[str, Any]]) -> str | None:
    for position, document in enumerate(documents):
        identifier = document.get("id")
        if not isinstance(identifier, str) or not identifier:
            return f"document at position {position} has no usable id"
    return None


def page_meta(page: Page) -> str | None:
    if page.next is not None:
        return json.dumps({"next": page.next})
    if page.page is not None or page.total_pages is not None:
        return json.dumps({"page": page.page, "totalPages": page.total_pages})
    return None


def page_is_valid(page: Page) -> tuple[bool, str | None]:
    if page.error is not None:
        return False, page.error
    error = id_error(page.documents)
    return error is None, error


def load_scope(connection: sqlite3.Connection, scope_id: int) -> Scope:
    row = connection.execute(
        "SELECT id, run_id, kind, parent, expected, outcome FROM scopes WHERE id = ?", (scope_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"scope {scope_id} does not exist")
    return Scope(*row)


def _document_id(connection: sqlite3.Connection, kind: str, provider_id: str) -> int:
    connection.execute(
        "INSERT INTO documents (kind, provider_id) VALUES (?,?) ON CONFLICT DO NOTHING",
        (kind, provider_id),
    )
    row = connection.execute(
        "SELECT id FROM documents WHERE kind = ? AND provider_id = ?", (kind, provider_id)
    ).fetchone()
    return int(row[0])


def derive_page(
    connection: sqlite3.Connection, scope: Scope, page: Page, received_at: str, received_day: str
) -> bool:
    """Derives one page once. Returns False when it had already been derived."""
    already = connection.execute(
        "SELECT 1 FROM page_derivations WHERE response_id = ?", (page.attempt_id,)
    ).fetchone()
    if already is not None:
        return False
    valid, error = page_is_valid(page)
    with transaction(connection):
        if valid:
            for position, document in enumerate(page.documents):
                sha, body = canonical(document)
                connection.execute(
                    "INSERT INTO versions (sha256, body) VALUES (?,?) ON CONFLICT DO NOTHING",
                    (sha, body),
                )
                connection.execute(
                    "INSERT INTO observations (run_id, scope_id, response_id, position,"
                    " document_id, parent, sha256, observed_at, observed_day)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        scope.run_id,
                        scope.id,
                        page.attempt_id,
                        position,
                        _document_id(connection, scope.kind, document["id"]),
                        scope.parent,
                        sha,
                        received_at,
                        received_day,
                    ),
                )
        connection.execute(
            "INSERT INTO page_derivations (response_id, valid, page_meta, error) VALUES (?,?,?,?)",
            (page.attempt_id, 1 if valid else 0, page_meta(page), error),
        )
    return True


def _received(connection: sqlite3.Connection, response_id: Any) -> tuple[str, str]:
    row = connection.execute(
        "SELECT received_at, received_day FROM responses WHERE id = ?", (response_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"page names response {response_id!r}, which is not archived")
    return str(row[0]), str(row[1])


def _date_bounds(kind: str, documents: list[dict[str, Any]]) -> tuple[str | None, str | None, bool]:
    """(min_date, min_instant, undated). `undated` is True when a document's
    date did not parse: no coverage is claimed over dates the walk cannot read."""
    if kind not in DATE_FIELDS:
        return None, None, False
    dates = [business_date(kind, document) for document in documents]
    if any(date is None for date in dates):
        return None, None, True
    min_instant = None
    if kind == "transactions":
        instants = [transaction_instant(document) for document in documents]
        if any(instant is None for instant in instants):
            return None, None, True
        min_instant = min(instants) if instants else None  # type: ignore[type-var]
    return (min(dates) if dates else None), min_instant, False  # type: ignore[type-var]


def derive_scope(
    connection: sqlite3.Connection, scope: Scope, result: FetchResult, fallback_time: str
) -> ScopeClose:
    """Derives every page of a finished walk and computes how the scope closes.
    Used by the live path and by the recovery sweep alike."""
    invalid: list[str] = []
    documents: list[dict[str, Any]] = []
    for number, page in enumerate(result.pages, start=1):
        received_at, received_day = _received(connection, page.attempt_id)
        derive_page(connection, scope, page, received_at, received_day)
        valid, error = page_is_valid(page)
        if valid:
            documents.extend(page.documents)
        else:
            invalid.append(f"page {number}: {error}")

    outcome = result.outcome
    if invalid and outcome != "unavailable":
        outcome = "incomplete"
    rows = connection.execute(
        "SELECT count(DISTINCT document_id) FROM observations WHERE scope_id = ?", (scope.id,)
    ).fetchone()[0]
    repeated = connection.execute(
        "SELECT count(*) FROM (SELECT document_id FROM observations WHERE scope_id = ?"
        " GROUP BY document_id HAVING count(*) > 1)",
        (scope.id,),
    ).fetchone()[0]
    min_date, min_instant, undated = _date_bounds(scope.kind, documents)
    last = connection.execute(
        "SELECT max(received_at) FROM responses WHERE scope_id = ?", (scope.id,)
    ).fetchone()[0]
    closed_at = str(last) if last is not None else fallback_time
    errors = [part for part in [result.error, *invalid] if part]
    return ScopeClose(
        outcome=outcome,
        infer_absence=1 if outcome == "complete" and not repeated and not undated else 0,
        status=result.status or None,
        rows=int(rows),
        min_date=min_date,
        min_instant=min_instant,
        closed_at=closed_at,
        closed_day=local_day(closed_at),
        error="; ".join(errors) if errors else None,
    )


def close_scope(connection: sqlite3.Connection, scope_id: int, close: ScopeClose) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE scopes SET outcome=?, infer_absence=?, status=?, rows=?, min_date=?,"
            " min_instant=?, closed_at=?, closed_day=?, error=? WHERE id=? AND outcome='pending'",
            (
                close.outcome,
                close.infer_absence,
                close.status,
                close.rows,
                close.min_date,
                close.min_instant,
                close.closed_at,
                close.closed_day,
                close.error,
                scope_id,
            ),
        )
