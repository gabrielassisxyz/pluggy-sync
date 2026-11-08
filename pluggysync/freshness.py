"""Whether what a run collected is current, per connection and per consent.

A run can close `complete` with every walk answered while the bank connection
behind it has been dead for a week: the provider keeps serving the last state
it has. Freshness reads the item and consent documents the run sighted and
names what is stale, and a stale item or consent makes the run `partial`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .dates import parse_instant

# The provider syncs each connection about once a day; 48 hours tolerates one
# missed cycle before the data is called stale.
STALE_AFTER_HOURS = 48
# A consent about to expire is a connection about to go dark; 30 days leaves
# time to renew it before any run is affected.
CONSENT_EXPIRES_WITHIN_DAYS = 30


@dataclass(frozen=True)
class Staleness:
    kind: str
    provider_id: str
    reason: str

    def line(self) -> str:
        return f"stale {self.kind} {self.provider_id}: {self.reason}"


def item_staleness(document: dict, now: datetime) -> str | None:
    status = document.get("status")
    if status != "UPDATED":
        return f"status {status!r}"
    execution = document.get("executionStatus")
    if execution != "SUCCESS":
        return f"executionStatus {execution!r}"
    updated = parse_instant(document.get("lastUpdatedAt"))
    if updated is None:
        return f"lastUpdatedAt {document.get('lastUpdatedAt')!r} unreadable"
    age = now - updated
    if age > timedelta(hours=STALE_AFTER_HOURS):
        return f"lastUpdatedAt {age.total_seconds() / 3600:.0f}h old (limit {STALE_AFTER_HOURS}h)"
    return None


def consent_staleness(document: dict, now: datetime) -> str | None:
    if document.get("revokedAt") is not None:
        return f"revokedAt {document.get('revokedAt')!r}"
    if document.get("expiresAt") is None:
        # The provider publishes no expiry for these consents (null on every real
        # one); an unknown expiry is not a looming one.
        return None
    expires = parse_instant(document.get("expiresAt"))
    if expires is None:
        return f"expiresAt {document.get('expiresAt')!r} unreadable"
    remaining = expires - now
    if remaining < timedelta(days=CONSENT_EXPIRES_WITHIN_DAYS):
        return (
            f"expiresAt in {remaining.total_seconds() / 86400:.0f} days"
            f" (limit {CONSENT_EXPIRES_WITHIN_DAYS})"
        )
    return None


def stale_in_run(connection: sqlite3.Connection, run_id: int, now: datetime) -> list[Staleness]:
    """Every item and consent the run sighted, judged on its last sighting in the run."""
    rows = connection.execute(
        "SELECT d.kind, d.provider_id, v.body FROM observations o"
        " JOIN documents d ON d.id = o.document_id JOIN versions v ON v.sha256 = o.sha256"
        " WHERE o.run_id = ? AND d.kind IN ('items', 'consents')"
        " AND o.id = (SELECT max(id) FROM observations WHERE run_id = o.run_id"
        "             AND document_id = o.document_id)"
        " ORDER BY d.kind, d.provider_id",
        (run_id,),
    ).fetchall()
    stale: list[Staleness] = []
    for kind, provider_id, body in rows:
        document = json.loads(body)
        judge = item_staleness if kind == "items" else consent_staleness
        reason = judge(document, now)
        if reason is not None:
            stale.append(Staleness("item" if kind == "items" else "consent", provider_id, reason))
    return stale
