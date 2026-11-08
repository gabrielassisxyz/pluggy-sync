"""One ledger run: sweep what a crash left, plan, walk in tiers, close honestly.

Parents are walked before children in tiers because a parent's ambiguity is a
run-wide property no single page can settle: only after every `investments`
and `accounts` walk of the run is derived are the child scopes decided. What
the run did not do is written down too, as `skipped` scopes, so a later reader
can tell "not fetched" from "fetched and empty".
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, urlsplit

from .archive import Archive, archived_responses
from .dates import parse_instant
from .derive import Scope, canonical, close_scope, derive_scope, load_scope
from .events import ambiguous_documents, project_pending_runs, project_run
from .freshness import stale_in_run
from .ledger import local_day, transaction
from .replay import replay_walk, scope_request, walk_scope
from .transport import OnAttempt
from .walk import Client, Clock

CrashHook = Callable[..., None]
PLANNED_PER_ITEM = ("items", "consents", "identities", "investments", "accounts")
CHILD_KINDS = {"investments": ("investment_transactions",), "accounts": ("transactions", "bills")}
PARENT_KINDS = tuple(CHILD_KINDS)
SWEPT = "swept: "


class ConfigurationError(Exception):
    """Exit code 2 territory: the environment, not the API, is wrong."""


class SelfCheckFailed(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_item_ids(raw: str, log: Callable[[str], None] | None = None) -> list[str]:
    """Deduplicated in first-seen order; a duplicate is logged, an empty id refused."""
    say = log or (lambda line: print(line, file=sys.stderr))
    seen: list[str] = []
    for position, piece in enumerate(raw.split(","), start=1):
        item_id = piece.strip()
        if not item_id:
            raise ConfigurationError(f"PLUGGY_ITEM_IDS has an empty id at position {position}")
        if item_id in seen:
            say(f"PLUGGY_ITEM_IDS names {item_id} more than once; using it once")
            continue
        seen.append(item_id)
    if not seen:
        raise ConfigurationError("PLUGGY_ITEM_IDS is empty")
    return seen


def code_version(script_dir: str) -> str:
    try:
        described = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=script_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return described.stdout.strip() if described.returncode == 0 and described.stdout else "unknown"


@dataclass(frozen=True)
class ChildPlan:
    kind: str
    parent: str
    expected: int
    walk: bool
    closed_at: str


class LedgerRun:
    def __init__(
        self,
        connection: sqlite3.Connection,
        item_ids: list[str],
        make_client: Callable[[OnAttempt], Client],
        clock: Clock,
        version: str = "unknown",
        crash_at: CrashHook | None = None,
    ) -> None:
        self._db = connection
        self._item_ids = item_ids
        self._make_client = make_client
        self._clock = clock
        self._version = version
        self._crash_at = crash_at or (lambda name, **context: None)

    # -- entry points ---------------------------------------------------------

    def sweep(self) -> list[int]:
        """Closes every run a crash left `running`, by the evidence it archived."""
        stale = [
            int(run_id)
            for (run_id,) in self._db.execute(
                "SELECT id FROM runs WHERE outcome='running' ORDER BY id"
            )
        ]
        now = self._clock.now()
        with transaction(self._db):
            for run_id in stale:
                self._settle_pending(run_id, now)
                self._insert_children(run_id, walk=False)
                self._close_run(run_id, outcome="interrupted", note=f"interrupted; swept at {now}")
            # A terminal run an earlier close could not project (its self-check
            # failed then) is tried again here, in order, before anything new runs.
            project_pending_runs(self._db, self_check)
        return stale

    def execute(self) -> int:
        run_id = self._insert_run()
        try:
            self._plan(run_id)
            self._crash_at("before_first_request", run_id=run_id)
            self._walk_tiers(run_id)
            self._close_run(run_id)
        except Exception as failure:
            self._fail(run_id, failure)
        return run_id

    # -- steps ----------------------------------------------------------------

    def _insert_run(self) -> int:
        started = self._clock.now()
        with transaction(self._db):
            cursor = self._db.execute(
                "INSERT INTO runs (started_at, started_day, outcome, code_version, item_ids)"
                " VALUES (?,?,?,?,?)",
                (started, local_day(started), "running", self._version, json.dumps(self._item_ids)),
            )
        return int(cursor.lastrowid or 0)

    def _plan(self, run_id: int) -> None:
        with transaction(self._db):
            self._insert_scope(run_id, "categories", "")
            for item_id in self._item_ids:
                for kind in PLANNED_PER_ITEM:
                    self._insert_scope(run_id, kind, item_id)

    def _walk_tiers(self, run_id: int) -> None:
        archive = Archive(self._db, run_id, self._crash_at)
        client = self._make_client(archive)
        self._walk(client, archive, self._scope(run_id, "categories", ""))
        for item_id in self._item_ids:
            self._walk(client, archive, self._scope(run_id, "items", item_id))
        for item_id in self._item_ids:
            item_scope = self._scope(run_id, "items", item_id)
            if item_scope.outcome != "complete":
                self._skip_planned(run_id, item_id, item_scope)
                continue
            for kind in PLANNED_PER_ITEM[1:]:
                self._walk(client, archive, self._scope(run_id, kind, item_id))
        children = self._insert_children(run_id, walk=True)
        for child in children:
            self._walk(client, archive, child)

    def _walk(self, client: Client, archive: Archive, scope: Scope) -> None:
        archive.scope_id = scope.id
        result = walk_scope(client, scope.kind, scope.parent)
        if archive.failure is not None:
            # The walk reports a raising callback as an incomplete page; a
            # database failure under the archive is the run's failure, not the walk's.
            raise archive.failure
        self._crash_at("before_scope_close", kind=scope.kind, parent=scope.parent)
        close = derive_scope(self._db, scope, result, self._clock.now())
        close_scope(self._db, scope.id, close)
        self._crash_at("after_scope_close", kind=scope.kind, parent=scope.parent)

    def _close_run(self, run_id: int, outcome: str | None = None, note: str | None = None) -> None:
        with transaction(self._db):
            if outcome is None:
                ambiguous = project_run(self._db, run_id)
                self._crash_at("during_run_close", run_id=run_id)
                problems = self_check(self._db, run_id)
                if problems:
                    raise SelfCheckFailed(problems)
                if self._authentication_refused(run_id):
                    # Nothing was collected and no walk could start: that is a failed
                    # run, not a partial one with every walk incomplete.
                    outcome, note = "failed", "authentication refused: no walk could start"
                else:
                    stale = self._stale(run_id)
                    outcome = (
                        "complete"
                        if self._every_owed_scope_done(run_id) and not ambiguous and not stale
                        else "partial"
                    )
                    if stale:
                        note = "; ".join(item.line() for item in stale)
            else:
                # Interrupted or failed: project what did commit, only if it checks out.
                problems = self_check(self._db, run_id)
                if problems:
                    note = f"{note + '; ' if note else ''}self-check: {'; '.join(problems)}"
                else:
                    project_run(self._db, run_id)
            last = self._db.execute(
                "SELECT max(received_at), count(*) FROM responses WHERE run_id = ?", (run_id,)
            ).fetchone()
            finished = str(last[0]) if last[0] is not None else self._clock.now()
            self._db.execute(
                "UPDATE runs SET finished_at=?, finished_day=?, api_calls=?, outcome=?, note=?"
                " WHERE id=? AND outcome='running'",
                (finished, local_day(finished), int(last[1]), outcome, note, run_id),
            )

    def _fail(self, run_id: int, failure: Exception) -> None:
        """What the run does when something it did not expect went wrong: the
        same closing work as the recovery sweep, under `failed`."""
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")
        now = self._clock.now()
        with transaction(self._db):
            self._settle_pending(run_id, now)
            self._insert_children(run_id, walk=False)
            self._close_run(run_id, outcome="failed", note=f"failed: {failure!r}")

    # -- recovery -------------------------------------------------------------

    def _settle_pending(self, run_id: int, now: str) -> None:
        """Closes every pending scope by replaying its archived attempts."""
        pending = self._db.execute(
            "SELECT id FROM scopes WHERE run_id = ? AND outcome = 'pending' ORDER BY id", (run_id,)
        ).fetchall()
        for (scope_id,) in pending:
            scope = load_scope(self._db, scope_id)
            result = replay_walk(scope.kind, scope.parent, archived_responses(self._db, scope.id))
            close = derive_scope(self._db, scope, result, now)
            if close.outcome == "incomplete":
                close = replace(close, error=SWEPT + (close.error or ""))
            close_scope(self._db, scope.id, close)

    # -- scopes ---------------------------------------------------------------

    def _insert_scope(
        self,
        run_id: int,
        kind: str,
        parent: str,
        expected: int = 1,
        outcome: str = "pending",
        closed_at: str | None = None,
    ) -> Scope:
        closed = (
            (closed_at, local_day(closed_at), 0) if outcome != "pending" else (None, None, None)
        )
        cursor = self._db.execute(
            "INSERT INTO scopes (run_id, kind, parent, expected, outcome, closed_at, closed_day,"
            " rows) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, kind, parent, expected, outcome, *closed),
        )
        return Scope(int(cursor.lastrowid or 0), run_id, kind, parent, expected, outcome)

    def _scope(self, run_id: int, kind: str, parent: str) -> Scope:
        row = self._db.execute(
            "SELECT id, run_id, kind, parent, expected, outcome FROM scopes"
            " WHERE run_id = ? AND kind = ? AND parent = ?",
            (run_id, kind, parent),
        ).fetchone()
        if row is None:
            raise LookupError(f"run {run_id} has no scope {kind}/{parent}")
        return Scope(*row)

    def _find_scope(self, run_id: int, kind: str, parent: str) -> Scope | None:
        try:
            return self._scope(run_id, kind, parent)
        except LookupError:
            return None

    def _closed_at(self, scope: Scope) -> str:
        row = self._db.execute("SELECT closed_at FROM scopes WHERE id = ?", (scope.id,)).fetchone()
        return str(row[0]) if row and row[0] is not None else self._clock.now()

    def _skip_planned(self, run_id: int, item_id: str, item_scope: Scope) -> None:
        """An item that did not answer owes its planned walks nothing it can do."""
        closed_at = self._closed_at(item_scope)
        with transaction(self._db):
            self._db.execute(
                "UPDATE scopes SET outcome='skipped', rows=0, closed_at=?, closed_day=?"
                " WHERE run_id=? AND parent=? AND outcome='pending' AND kind IN (?,?,?,?)",
                (closed_at, local_day(closed_at), run_id, item_id, *PLANNED_PER_ITEM[1:]),
            )

    # -- children -------------------------------------------------------------

    def _insert_children(self, run_id: int, walk: bool) -> list[Scope]:
        """Inserts the child scopes the run owes. With `walk`, the ones it can
        walk are inserted pending and returned; the rest are closed skipped."""
        walkable: list[Scope] = []
        with transaction(self._db):
            for child in self._plan_children(run_id):
                if self._find_scope(run_id, child.kind, child.parent) is not None:
                    continue
                if walk and child.walk:
                    walkable.append(
                        self._insert_scope(run_id, child.kind, child.parent, child.expected)
                    )
                else:
                    self._insert_scope(
                        run_id, child.kind, child.parent, child.expected, "skipped", child.closed_at
                    )
        return walkable

    def _plan_children(self, run_id: int) -> list[ChildPlan]:
        ambiguous = ambiguous_documents(self._db, run_id)
        plans: list[ChildPlan] = []
        for item_id in self._item_ids:
            item_scope = self._find_scope(run_id, "items", item_id)
            item_closed = self._closed_at(item_scope) if item_scope else self._clock.now()
            for kind in PARENT_KINDS:
                parent_scope = self._find_scope(run_id, kind, item_id)
                plans.extend(
                    self._children_of(run_id, kind, item_id, parent_scope, ambiguous, item_closed)
                )
        return plans

    def _children_of(
        self,
        run_id: int,
        kind: str,
        item_id: str,
        parent_scope: Scope | None,
        ambiguous: dict[int, tuple[list[str], str]],
        item_closed: str,
    ) -> list[ChildPlan]:
        known = present_parents(self._db, kind, item_id, run_id)
        walked = parent_scope is not None and parent_scope.outcome not in ("pending", "skipped")
        if not walked:
            # No usable parent walk this run: every known parent is owed and unfetched.
            closed_at = self._closed_at(parent_scope) if parent_scope else item_closed
            return [
                plan
                for provider_id, credit in known.items()
                for plan in _child_plans(kind, provider_id, credit, 1, False, closed_at)
            ]
        assert parent_scope is not None
        closed_at = self._closed_at(parent_scope)
        row = self._db.execute(
            "SELECT outcome, infer_absence FROM scopes WHERE id = ?", (parent_scope.id,)
        ).fetchone()
        absence_safe = row[0] == "complete" and row[1] == 1
        plans: list[ChildPlan] = []
        seen = sightings_in_scope(self._db, parent_scope.id)
        for provider_id, (document_id, credit) in seen.items():
            if document_id in ambiguous:
                credit = credit or known.get(provider_id, False)
                plans.extend(_child_plans(kind, provider_id, credit, 1, False, closed_at))
            else:
                plans.extend(_child_plans(kind, provider_id, credit, 1, True, closed_at))
        for provider_id, credit in known.items():
            if provider_id in seen:
                continue
            # Omitted by a walk that proves absence: not owed. Otherwise owed, unfetched.
            expected = 0 if absence_safe else 1
            plans.extend(_child_plans(kind, provider_id, credit, expected, False, closed_at))
        return plans

    # -- run close ------------------------------------------------------------

    def _stale(self, run_id: int) -> list:
        started = self._db.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
        now = parse_instant(started[0]) if started else None
        if now is None:
            return []
        return stale_in_run(self._db, run_id, now)

    def _authentication_refused(self, run_id: int) -> bool:
        responses = self._db.execute(
            "SELECT count(*) FROM responses WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        refused, answered = self._db.execute(
            "SELECT sum(CASE WHEN error LIKE 'auth: %' THEN 1 ELSE 0 END),"
            " sum(CASE WHEN outcome IN ('complete', 'unavailable') THEN 1 ELSE 0 END)"
            " FROM scopes WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(responses) == 0 and int(refused or 0) > 0 and int(answered or 0) == 0

    def summary(self, run_id: int) -> list[str]:
        """One line per owed walk that did not finish and per stale item or consent."""
        lines = [
            f"{kind} {parent} {outcome}{': ' + error if error else ''}"
            for kind, parent, outcome, error in self._db.execute(
                "SELECT kind, parent, outcome, error FROM scopes WHERE run_id = ? AND expected = 1"
                " AND outcome IN ('incomplete', 'skipped', 'pending') ORDER BY id",
                (run_id,),
            )
        ]
        lines.extend(item.line() for item in self._stale(run_id))
        return lines

    def _every_owed_scope_done(self, run_id: int) -> bool:
        undone = self._db.execute(
            "SELECT count(*) FROM scopes WHERE run_id = ? AND expected = 1"
            " AND outcome NOT IN ('complete', 'unavailable')",
            (run_id,),
        ).fetchone()[0]
        return int(undone) == 0


def _child_plans(
    kind: str, provider_id: str, credit: bool, expected: int, walk: bool, closed_at: str
) -> list[ChildPlan]:
    kinds = [child for child in CHILD_KINDS[kind] if child != "bills" or credit]
    return [ChildPlan(child, provider_id, expected, walk, closed_at) for child in kinds]


def _is_credit(body: str) -> bool:
    return json.loads(body).get("type") == "CREDIT"


def sightings_in_scope(
    connection: sqlite3.Connection, scope_id: int
) -> dict[str, tuple[int, bool]]:
    """provider_id -> (document_id, seen as CREDIT in any sighting of this scope)."""
    rows = connection.execute(
        "SELECT d.provider_id, d.id, v.body FROM observations o"
        " JOIN documents d ON d.id = o.document_id JOIN versions v ON v.sha256 = o.sha256"
        " WHERE o.scope_id = ? ORDER BY o.id",
        (scope_id,),
    ).fetchall()
    seen: dict[str, tuple[int, bool]] = {}
    for provider_id, document_id, body in rows:
        credit = seen.get(provider_id, (document_id, False))[1] or _is_credit(body)
        seen[provider_id] = (document_id, credit)
    return seen


def present_parents(
    connection: sqlite3.Connection, kind: str, item_id: str, before_run: int
) -> dict[str, bool]:
    """Parents of `kind` under `item_id` the ledger holds present before this
    run: provider_id -> last sighting was CREDIT. Read from `document_state`,
    which the run being planned has not been projected into yet."""
    rows = connection.execute(
        "SELECT d.provider_id, v.body FROM document_state ds"
        " JOIN documents d ON d.id = ds.document_id JOIN versions v ON v.sha256 = ds.sha256"
        " WHERE d.kind = ? AND ds.parent = ? AND ds.state = 'present' AND ds.last_seen_run < ?",
        (kind, item_id, before_run),
    ).fetchall()
    return {provider_id: _is_credit(body) for provider_id, body in rows}


# -- the per-run self-check -----------------------------------------------------


def url_matches_scope(kind: str, parent: str, url: str) -> bool:
    request = scope_request(kind, parent)
    expected = urlsplit(request.path + request.query)
    actual = urlsplit(url)
    if actual.path != expected.path:
        return False
    wanted = parse_qs(expected.query)
    got = parse_qs(actual.query)
    return all(got.get(name) == value for name, value in wanted.items())


def self_check(connection: sqlite3.Connection, run_id: int) -> list[str]:
    """The cross-links a trigger cannot express, over this run's rows."""
    problems: list[str] = []
    counts = {
        "responses whose run differs from their scope's": (
            "SELECT count(*) FROM responses r JOIN scopes s ON s.id = r.scope_id"
            " WHERE r.run_id = ? AND s.run_id <> r.run_id"
        ),
        "observations whose run or parent differs from their scope's": (
            "SELECT count(*) FROM observations o JOIN scopes s ON s.id = o.scope_id"
            " WHERE o.run_id = ? AND (s.run_id <> o.run_id OR s.parent <> o.parent)"
        ),
        "observations disagreeing with their response": (
            "SELECT count(*) FROM observations o JOIN responses r ON r.id = o.response_id"
            " WHERE o.run_id = ? AND (r.run_id <> o.run_id OR r.scope_id <> o.scope_id"
            " OR r.received_at <> o.observed_at OR r.received_day <> o.observed_day)"
        ),
        "observations whose document kind differs from their scope's": (
            "SELECT count(*) FROM observations o JOIN documents d ON d.id = o.document_id"
            " JOIN scopes s ON s.id = o.scope_id WHERE o.run_id = ? AND d.kind <> s.kind"
        ),
        "observations whose body id differs from the document's provider id": (
            "SELECT count(*) FROM observations o JOIN documents d ON d.id = o.document_id"
            " JOIN versions v ON v.sha256 = o.sha256"
            " WHERE o.run_id = ? AND json_extract(v.body, '$.id') IS NOT d.provider_id"
        ),
        "closed scopes whose rows differ from their distinct documents": (
            "SELECT count(*) FROM scopes s WHERE s.run_id = ? AND s.outcome <> 'pending'"
            " AND s.rows <> (SELECT count(DISTINCT document_id) FROM observations"
            "                WHERE scope_id = s.id)"
        ),
        "pending scopes": "SELECT count(*) FROM scopes WHERE run_id = ? AND outcome = 'pending'",
        "observations under skipped or unavailable scopes": (
            "SELECT count(*) FROM observations o JOIN scopes s ON s.id = o.scope_id"
            " WHERE o.run_id = ? AND s.outcome IN ('skipped', 'unavailable')"
        ),
    }
    for description, sql in counts.items():
        count = int(connection.execute(sql, (run_id,)).fetchone()[0])
        if count:
            problems.append(f"{count} {description}")
    mismatched = [
        url
        for url, kind, parent in connection.execute(
            "SELECT r.url, s.kind, s.parent FROM responses r JOIN scopes s ON s.id = r.scope_id"
            " WHERE r.run_id = ?",
            (run_id,),
        )
        if not url_matches_scope(kind, parent, url)
    ]
    if mismatched:
        problems.append(f"{len(mismatched)} archived URLs not matching their scope")
    for sha, body in connection.execute(
        "SELECT DISTINCT v.sha256, v.body FROM observations o"
        " JOIN versions v ON v.sha256 = o.sha256 WHERE o.run_id = ?",
        (run_id,),
    ):
        recomputed, canonical_body = canonical(json.loads(body))
        if recomputed != sha or canonical_body != body:
            problems.append(f"version {sha} is not the canonical hash of its body")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        problems.append("foreign_key_check reports violations")
    return problems
