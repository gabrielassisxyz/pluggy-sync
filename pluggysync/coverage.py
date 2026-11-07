"""What the last absence-safe walk of each `(kind, parent)` vouched for.

Coverage is by range: a complete walk of a dated kind vouches only for the
documents whose business date is on or after the earliest date it returned.
Anything older is simply not covered, and nothing is inferred about it. The
comparison is always against the projected run, never "the latest complete
run": after a failed run in which one account's walk finished and another's
did not, that other comparison inverts the answer.
"""

from __future__ import annotations

import json
import sqlite3

from .dates import DATE_FIELDS, business_date

# One row per (kind, parent) ever walked. `last_min_date` belongs to the last
# absence-safe walk, never to a newer one: a newer completion is never paired
# with an older range.
SCOPE_COVERAGE_VIEW = """CREATE VIEW scope_coverage AS
SELECT
  k.kind,
  k.parent,
  (SELECT max(run_id) FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent AND s.outcome = 'complete') AS last_complete_run,
  (SELECT closed_at FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent AND s.outcome = 'complete'
    ORDER BY run_id DESC LIMIT 1) AS last_complete_at,
  (SELECT max(run_id) FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent AND s.infer_absence = 1) AS last_absence_safe_run,
  (SELECT min_date FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent AND s.infer_absence = 1
    ORDER BY run_id DESC LIMIT 1) AS last_min_date,
  (SELECT max(run_id) FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent) AS last_attempt_run,
  (SELECT outcome FROM scopes s
    WHERE s.kind = k.kind AND s.parent = k.parent ORDER BY run_id DESC LIMIT 1) AS last_outcome
FROM (SELECT DISTINCT kind, parent FROM scopes) k"""


def projected_run(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT projected_run FROM projection_state WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def is_covered(connection: sqlite3.Connection, document_id: int) -> bool:
    """Covered: the document's (kind, parent) was walked absence-safe by the
    projected run and, for a dated kind, its business date is inside that
    walk's range."""
    row = connection.execute(
        "SELECT d.kind, ds.parent, v.body FROM document_state ds"
        " JOIN documents d ON d.id = ds.document_id JOIN versions v ON v.sha256 = ds.sha256"
        " WHERE ds.document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        return False
    kind, parent, body = row
    coverage = connection.execute(
        "SELECT last_absence_safe_run, last_min_date FROM scope_coverage"
        " WHERE kind = ? AND parent = ?",
        (kind, parent),
    ).fetchone()
    if coverage is None or coverage[0] is None or coverage[0] != projected_run(connection):
        return False
    if kind not in DATE_FIELDS:
        return True
    if coverage[1] is None:
        return False
    date = business_date(kind, json.loads(body))
    return date is not None and date >= coverage[1]
