"""Events, disappearance, ambiguity and the rebuild, over hand-built sightings."""

from __future__ import annotations

import sqlite3
import unittest

from pluggysync.coverage import projected_run
from pluggysync.events import ProjectionBlocked, rebuild_projection
from pluggysync.ledger import transaction
from pluggysync.run import LedgerRun
from pluggysync.walk import BASE
from tests.fakes import ok
from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ACCOUNT_A2,
    ITEM_A,
    ITEM_B,
    PAGE_1,
    LedgerHarness,
    account_doc,
    script_two_items,
    tx_doc,
)
from tests.fixtures.pages import cursor_page, cursor_query, paged_page

ACC = "acc-0001"
OTHER = "acc-0002"


def tx(tx_id: str, date: str = "2026-01-10T12:00:00.000Z", amount: float = -10.0, **extra) -> dict:
    return {"id": tx_id, "date": date, "amount": amount, "status": "POSTED", **extra}


class EventCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = HandBuilt()
        self.addCleanup(self.ledger.close)

    def account_walk(
        self, run_id: int, parent: str = ACC, min_date: str | None = "2026-01-01", **kw
    ) -> int:
        return self.ledger.scope(run_id, "transactions", parent, min_date=min_date, **kw)


class SightingRulesTest(EventCase):
    def test_first_sighting_appears(self) -> None:
        run = self.ledger.run()
        self.ledger.sight(run, self.account_walk(run), tx("t1"))
        self.ledger.project(run)

        (event,) = self.ledger.events(run)
        self.assertEqual((event[2], event[3], event[4], event[6]), (1, "appeared", None, None))
        self.assertEqual(event[7], ACC)
        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "present")

    def test_same_hash_and_parent_is_no_event(self) -> None:
        for _ in range(2):
            run = self.ledger.run()
            self.ledger.sight(run, self.account_walk(run), tx("t1"))
            self.ledger.project(run)

        self.assertEqual(self.ledger.labels(run, "t1"), [])
        self.assertEqual(self.ledger.state("transactions", "t1")["last_seen_run"], run)
        self.assertEqual(self.ledger.state("transactions", "t1")["first_seen_run"], run - 1)

    def test_different_hash_is_changed_with_old_and_new(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.sight(run2, self.account_walk(run2), tx("t1", amount=-11.0))
        self.ledger.project(run2)

        (event,) = self.ledger.events(run2)
        self.assertEqual(event[3], "changed")
        self.assertNotEqual(event[4], event[5])
        self.assertEqual(
            event[4],
            self.ledger.db.execute(
                "SELECT sha256 FROM observations WHERE run_id = ?", (run1,)
            ).fetchone()[0],
        )

    def test_different_parent_is_moved_then_changed_when_the_hash_also_differs(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.sight(run2, self.account_walk(run2, parent=OTHER), tx("t1", amount=-12.0))
        self.ledger.project(run2)

        events = self.ledger.events(run2)
        self.assertEqual([(e[2], e[3]) for e in events], [(1, "moved"), (2, "changed")])
        self.assertEqual((events[0][6], events[0][7]), (ACC, OTHER))
        self.assertEqual(self.ledger.state("transactions", "t1")["parent"], OTHER)

    def test_same_hash_new_parent_is_only_moved(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.sight(run2, self.account_walk(run2, parent=OTHER), tx("t1"))
        self.ledger.project(run2)

        self.assertEqual(self.ledger.labels(run2, "t1"), ["moved"])

    def test_removed_document_reappears(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.account_walk(run2)
        self.ledger.project(run2)
        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "removed")
        run3 = self.ledger.run()
        self.ledger.sight(run3, self.account_walk(run3), tx("t1"))
        self.ledger.project(run3)

        self.assertEqual(self.ledger.labels(run3, "t1"), ["reappeared"])
        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "present")

    def test_removed_document_reappearing_elsewhere_with_a_new_body_is_moved_then_reappeared(
        self,
    ) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.account_walk(run2)
        self.ledger.project(run2)
        run3 = self.ledger.run()
        self.ledger.sight(run3, self.account_walk(run3, parent=OTHER), tx("t1", amount=-13.0))
        self.ledger.project(run3)

        events = self.ledger.events(run3)
        self.assertEqual([(e[2], e[3]) for e in events], [(1, "moved"), (2, "reappeared")])
        self.assertEqual((events[1][6], events[1][7]), (ACC, OTHER))

    def test_first_seen_ambiguously_appears_on_resolution_whichever_sighting_came_last(
        self,
    ) -> None:
        outcomes = []
        for order in ("a-then-b", "b-then-a"):
            ledger = HandBuilt()
            self.addCleanup(ledger.close)
            run1 = ledger.run()
            scope = ledger.scope(run1, "transactions", ACC, min_date="2026-01-01")
            bodies = [tx("t1"), tx("t1", amount=-20.0)]
            if order == "b-then-a":
                bodies.reverse()
            for body in bodies:
                ledger.sight(run1, scope, body)
            ledger.project(run1)
            self.assertEqual(ledger.events(run1), [])
            self.assertEqual(ledger.state("transactions", "t1")["ambiguous_run"], run1)
            run2 = ledger.run()
            ledger.sight(
                run2, ledger.scope(run2, "transactions", ACC, min_date="2026-01-01"), tx("t1")
            )
            ledger.project(run2)
            outcomes.append([e[1:] for e in ledger.events(run2)])
            self.assertEqual(ledger.labels(run2, "t1"), ["appeared"])
            self.assertIsNone(ledger.state("transactions", "t1")["ambiguous_run"])
        self.assertEqual(outcomes[0], outcomes[1])


class CoverageByRangeTest(EventCase):
    def test_absence_is_inferred_only_on_or_after_min_date(self) -> None:
        run1 = self.ledger.run()
        scope = self.account_walk(run1, min_date="2025-12-01")
        self.ledger.sight(run1, scope, tx("old", date="2025-12-20T12:00:00.000Z"))
        self.ledger.sight(run1, scope, tx("recent", date="2026-01-15T12:00:00.000Z"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.account_walk(run2, min_date="2026-01-01")
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("transactions", "old")["state"], "present")
        self.assertEqual(self.ledger.state("transactions", "recent")["state"], "removed")
        (event,) = self.ledger.events(run2)
        closed_day = self.ledger.db.execute(
            "SELECT closed_day FROM scopes WHERE run_id=?", (run2,)
        ).fetchone()[0]
        self.assertEqual((event[3], event[9]), ("removed", closed_day))
        self.assertEqual(event[5], None)

    def test_an_empty_complete_walk_infers_nothing_for_a_dated_kind(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.account_walk(run2, min_date=None)
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "present")
        self.assertEqual(self.ledger.events(run2), [])

    def test_a_walk_without_infer_absence_infers_nothing(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.account_walk(run2, infer_absence=0)
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "present")

    def test_replaced_pending_is_removed_when_the_walk_reaches_its_date(self) -> None:
        for min_date, expected in (("2025-09-01", "removed"), ("2026-10-01", "present")):
            ledger = HandBuilt()
            self.addCleanup(ledger.close)
            run1 = ledger.run()
            ledger.sight(
                run1,
                ledger.scope(run1, "transactions", ACC, min_date="2025-09-01"),
                tx("pending-1", date="2026-09-08T15:00:00.000Z", status="PENDING"),
            )
            ledger.project(run1)
            run2 = ledger.run()
            scope = ledger.scope(run2, "transactions", ACC, min_date=min_date)
            ledger.sight(run2, scope, tx("posted-1", date="2026-09-08T18:00:00.000Z"))
            ledger.project(run2)

            self.assertEqual(ledger.state("transactions", "pending-1")["state"], expected, min_date)

    def test_an_undated_kind_is_covered_whole(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1, self.ledger.scope(run1, "accounts", ITEM_A), account_doc(ACC, ITEM_A)
        )
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.scope(run2, "accounts", ITEM_A)
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("accounts", ACC)["state"], "removed")


class AmbiguousDisappearanceTest(EventCase):
    def bill(self) -> dict:
        return {"id": "bill-1", "dueDate": "2026-02-10T00:00:00.000Z", "totalAmount": 1.0}

    def ambiguous_run(self) -> int:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1, self.ledger.scope(run1, "bills", ACC, min_date="2026-01-01"), self.bill()
        )
        self.ledger.sight(
            run1, self.ledger.scope(run1, "bills", OTHER, min_date="2026-01-01"), self.bill()
        )
        self.ledger.project(run1)
        self.assertEqual(self.ledger.state("bills", "bill-1")["ambiguous_run"], run1)
        return run1

    def test_removed_only_when_every_candidate_parent_was_walked_absence_safe(self) -> None:
        self.ambiguous_run()
        run2 = self.ledger.run()
        self.ledger.scope(run2, "bills", ACC, min_date="2026-01-01")
        self.ledger.scope(run2, "bills", OTHER, outcome="incomplete")
        self.ledger.project(run2)
        self.assertEqual(self.ledger.state("bills", "bill-1")["state"], "present")
        self.assertEqual(self.ledger.events(run2), [])

        run3 = self.ledger.run()
        self.ledger.scope(run3, "bills", OTHER, min_date="2026-01-01")
        self.ledger.scope(run3, "bills", ACC, min_date="2026-01-01")
        self.ledger.project(run3)
        self.assertEqual(self.ledger.state("bills", "bill-1")["state"], "removed")
        self.assertEqual(self.ledger.labels(run3, "bill-1"), ["removed"])

    def test_candidate_parents_walks_must_reach_the_sighted_dates(self) -> None:
        self.ambiguous_run()
        run2 = self.ledger.run()
        self.ledger.scope(run2, "bills", ACC, min_date="2026-01-01")
        self.ledger.scope(run2, "bills", OTHER, min_date="2026-03-01")
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("bills", "bill-1")["state"], "present")

    def test_one_candidate_parent_removed_does_not_propagate_to_the_ambiguous_child(self) -> None:
        run1 = self.ledger.run()
        accounts = self.ledger.scope(run1, "accounts", ITEM_A)
        self.ledger.sight(run1, accounts, account_doc(ACC, ITEM_A, "CREDIT"))
        self.ledger.sight(run1, accounts, account_doc(OTHER, ITEM_A, "CREDIT"))
        self.ledger.sight(
            run1, self.ledger.scope(run1, "bills", ACC, min_date="2026-01-01"), self.bill()
        )
        self.ledger.sight(
            run1, self.ledger.scope(run1, "bills", OTHER, min_date="2026-01-01"), self.bill()
        )
        self.ledger.project(run1)
        run2 = self.ledger.run()
        accounts2 = self.ledger.scope(run2, "accounts", ITEM_A)
        self.ledger.sight(run2, accounts2, account_doc(OTHER, ITEM_A, "CREDIT"))
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("accounts", ACC)["state"], "removed")
        self.assertEqual(self.ledger.state("bills", "bill-1")["state"], "present")


class PropagationTest(EventCase):
    def seed_account_with_children(self) -> int:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1, self.ledger.scope(run1, "accounts", ITEM_A), account_doc(ACC, ITEM_A, "CREDIT")
        )
        self.ledger.sight(
            run1, self.ledger.scope(run1, "transactions", ACC, min_date="2026-01-01"), tx("t1")
        )
        self.ledger.sight(
            run1,
            self.ledger.scope(run1, "bills", ACC, min_date="2026-01-01"),
            {"id": "bill-1", "dueDate": "2026-02-10T00:00:00.000Z"},
        )
        inv = self.ledger.scope(run1, "investments", ITEM_A)
        self.ledger.sight(run1, inv, {"id": "inv-1", "type": "FIXED_INCOME"})
        self.ledger.sight(
            run1,
            self.ledger.scope(run1, "investment_transactions", "inv-1", min_date="2026-01-01"),
            {"id": "itx-1", "date": "2026-01-03T00:00:00.000Z"},
        )
        self.ledger.project(run1)
        return run1

    def test_a_removed_account_or_investment_orphans_its_present_children(self) -> None:
        self.seed_account_with_children()
        run2 = self.ledger.run()
        self.ledger.scope(run2, "accounts", ITEM_A)
        self.ledger.scope(run2, "investments", ITEM_A)
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("accounts", ACC)["state"], "removed")
        for kind, pid in (
            ("transactions", "t1"),
            ("bills", "bill-1"),
            ("investment_transactions", "itx-1"),
        ):
            self.assertEqual(self.ledger.state(kind, pid)["state"], "parent_gone", pid)
            self.assertEqual(self.ledger.labels(run2, pid), ["parent_gone"])
        account_event = next(e for e in self.ledger.events(run2) if e[1] == ACC)
        child_event = next(e for e in self.ledger.events(run2) if e[1] == "t1")
        self.assertEqual(account_event[8:], child_event[8:])

    def test_a_refused_item_removes_nothing_and_cascades_nothing(self) -> None:
        self.seed_account_with_children()
        run2 = self.ledger.run(outcome="partial")
        self.ledger.scope(run2, "items", ITEM_A, outcome="incomplete")
        self.ledger.scope(run2, "accounts", ITEM_A, outcome="skipped", infer_absence=0)
        self.ledger.project(run2)

        self.assertEqual(self.ledger.events(run2), [])
        self.assertEqual(self.ledger.state("accounts", ACC)["state"], "present")
        self.assertEqual(self.ledger.state("transactions", "t1")["state"], "present")


class OrderIndependenceTest(EventCase):
    def test_walk_order_within_a_run_does_not_change_events_or_ambiguities(self) -> None:
        results = []
        for reverse in (False, True):
            ledger = HandBuilt()
            self.addCleanup(ledger.close)
            run1 = ledger.run()
            steps = [
                (
                    "accounts",
                    ITEM_A,
                    [account_doc(ACC, ITEM_A, "CREDIT"), account_doc(OTHER, ITEM_A, "CREDIT")],
                ),
                ("transactions", ACC, [tx("t1"), tx("shared", amount=-1.0)]),
                ("transactions", OTHER, [tx("t2"), tx("shared", amount=-2.0)]),
                ("bills", ACC, [{"id": "bill-1", "dueDate": "2026-02-10T00:00:00.000Z"}]),
            ]
            for kind, parent, bodies in reversed(steps) if reverse else steps:
                scope = ledger.scope(
                    run1, kind, parent, min_date="2026-01-01" if kind != "accounts" else None
                )
                for body in bodies:
                    ledger.sight(run1, scope, body)
            ledger.project(run1)
            run2 = ledger.run()
            for kind, parent in (
                ("accounts", ITEM_A),
                ("transactions", ACC),
                ("transactions", OTHER),
                ("bills", ACC),
            ):
                ledger.scope(
                    run2, kind, parent, min_date="2026-01-01" if kind != "accounts" else None
                )
            ledger.project(run2)
            events = [e[1:] for e in ledger.events()]
            ambiguities = ledger.db.execute(
                "SELECT d.provider_id, a.candidate_parents, a.reason FROM ambiguities a"
                " JOIN documents d ON d.id = a.document_id ORDER BY a.run_id, d.provider_id"
            ).fetchall()
            results.append((events, ambiguities))
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            results[0][1],
            [("shared", f'["{ACC}", "{OTHER}"]', "two bodies in one run; two parents in one run")],
        )


class ProjectionBlockedTest(EventCase):
    def test_a_terminal_run_failing_its_self_check_blocks_the_checkpoint(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.account_walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run(outcome="failed")
        scope = self.account_walk(run2)
        # An observation whose parent disagrees with its scope: a crossed link.
        self.ledger.sight(run2, scope, tx("t2"), parent=OTHER)

        with self.assertRaises(ProjectionBlocked) as blocked:
            LedgerRun(self.ledger.db, [], lambda cb: None, _StillClock()).sweep()  # type: ignore[arg-type,return-value]

        self.assertEqual(blocked.exception.run_id, run2)
        self.assertEqual(projected_run(self.ledger.db), run1)
        self.assertEqual(self.ledger.events(run2), [])


class _StillClock:
    def sleep(self, seconds: float) -> None:
        return

    def now(self) -> str:
        return "2026-01-09T12:00:00+00:00"


class RebuildTest(unittest.TestCase):
    def test_rebuilding_five_runs_with_a_crash_matches_the_incremental_projection(self) -> None:
        harness = LedgerHarness()
        self.addCleanup(harness.close)
        items = [ITEM_A, ITEM_B]

        class Crash(BaseException):
            pass

        def crash(point: str, **context) -> None:
            if (
                point == "after_scope_close"
                and context.get("kind") == "accounts"
                and context.get("parent") == ITEM_A
            ):
                raise Crash()

        for number in range(1, 6):
            transport = harness.transport()
            script_two_items(transport)
            if number == 2:
                transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
                transport.answer(
                    f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
                    ok(
                        cursor_page(
                            [tx_doc("tx-a1-0001", ACCOUNT_A1), tx_doc("tx-a1-0009", ACCOUNT_A1)]
                        )
                    ),
                )
            if number == 5:
                transport.script[f"{BASE}/accounts?itemId={ITEM_A}&{PAGE_1}"].clear()
                transport.answer(
                    f"/accounts?itemId={ITEM_A}&{PAGE_1}",
                    ok(paged_page([account_doc(ACCOUNT_A1, ITEM_A)])),
                )
            if number == 3:
                with self.assertRaises(Crash):
                    harness.run(transport, items, crash_at=crash).execute()
                harness.reopen()
                self.assertEqual(len(harness.run(harness.transport(), items).sweep()), 1)
                continue
            harness.run(transport, items).execute()

        incremental = snapshot(harness.connection)
        self.assertEqual(harness.run_row(5)["outcome"], "complete")
        self.assertEqual(
            harness.connection.execute(
                "SELECT ds.state FROM document_state ds JOIN documents d ON d.id = ds.document_id"
                " WHERE d.provider_id = ?",
                (ACCOUNT_A2,),
            ).fetchone()[0],
            "removed",
        )
        with transaction(harness.connection):
            rebuilt_runs = rebuild_projection(harness.connection)

        self.assertEqual(rebuilt_runs, [1, 2, 3, 4, 5])
        self.assertEqual(snapshot(harness.connection), incremental)
        self.assertTrue(any(row[3] == "parent_gone" for row in incremental[0]), incremental[0])


def snapshot(connection: sqlite3.Connection) -> tuple[list[tuple], ...]:
    return (
        connection.execute("SELECT * FROM events ORDER BY run_id, document_id, seq").fetchall(),
        connection.execute("SELECT * FROM ambiguities ORDER BY run_id, document_id").fetchall(),
        connection.execute("SELECT * FROM document_state ORDER BY document_id").fetchall(),
        connection.execute("SELECT * FROM projection_state").fetchall(),
    )


if __name__ == "__main__":
    unittest.main()
