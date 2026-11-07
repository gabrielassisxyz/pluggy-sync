"""What the last absence-safe walk covered, compared against the projected run."""

from __future__ import annotations

import sqlite3
import unittest

from pluggysync.coverage import is_covered, projected_run
from pluggysync.walk import BASE
from tests.fakes import ok
from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ACCOUNT_B1,
    ITEM_A,
    ITEM_B,
    LedgerHarness,
    script_two_items,
    tx_doc,
)
from tests.fixtures.pages import cursor_page, cursor_query

ACC = "acc-0001"


def coverage_row(connection: sqlite3.Connection, kind: str, parent: str) -> tuple:
    return connection.execute(
        "SELECT last_complete_run, last_absence_safe_run, last_min_date, last_attempt_run,"
        " last_outcome FROM scope_coverage WHERE kind = ? AND parent = ?",
        (kind, parent),
    ).fetchone()


def document_id(connection: sqlite3.Connection, kind: str, provider_id: str) -> int:
    return int(
        connection.execute(
            "SELECT id FROM documents WHERE kind = ? AND provider_id = ?", (kind, provider_id)
        ).fetchone()[0]
    )


class ScopeCoverageViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = HandBuilt()
        self.addCleanup(self.ledger.close)

    def test_last_min_date_belongs_to_the_last_absence_safe_walk_not_a_newer_one(self) -> None:
        run1 = self.ledger.run()
        self.ledger.scope(run1, "transactions", ACC, min_date="2025-06-01")
        run2 = self.ledger.run()
        self.ledger.scope(run2, "transactions", ACC, infer_absence=0, min_date="2025-12-01")
        run3 = self.ledger.run()
        self.ledger.scope(run3, "transactions", ACC, outcome="incomplete", min_date="2026-01-01")

        row = coverage_row(self.ledger.db, "transactions", ACC)

        self.assertEqual(row, (run2, run1, "2025-06-01", run3, "incomplete"))


class CoveredDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        self.items = [ITEM_A, ITEM_B]

    def test_after_a_failed_run_the_finished_account_is_covered_and_the_other_is_not(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.harness.run(first, self.items).execute()
        second = self.harness.transport()
        script_two_items(second)
        # B's walk has two pages; the failure lands after page 1 is archived, so the
        # archive holds a page with a cursor and no page after it: not finished.
        first_page = f"{BASE}/v2/transactions{cursor_query(ACCOUNT_B1)}"
        second.script[first_page].clear()
        second.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_B1)}",
            ok(cursor_page([tx_doc("tx-b1-0001", ACCOUNT_B1)], cursor_query(ACCOUNT_B1, "c1"))),
        )

        def fail_on_b(point: str, **context) -> None:
            if point == "after_archive_commit" and context["url"] == first_page:
                raise sqlite3.OperationalError("disk I/O error")

        run_id = self.harness.run(second, self.items, crash_at=fail_on_b).execute()

        db = self.harness.connection
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "failed")
        self.assertEqual(projected_run(db), run_id)
        self.assertTrue(is_covered(db, document_id(db, "transactions", "tx-a1-0001")))
        self.assertFalse(is_covered(db, document_id(db, "transactions", "tx-b1-0001")))
        self.assertEqual(coverage_row(db, "transactions", ACCOUNT_B1)[1], run_id - 1)
        self.assertEqual(coverage_row(db, "transactions", ACCOUNT_B1)[4], "incomplete")

    def test_a_document_dated_before_the_range_is_not_covered(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        run_id = self.harness.run(transport, self.items).execute()
        db = self.harness.connection
        tx = document_id(db, "transactions", "tx-a1-0001")
        self.assertTrue(is_covered(db, tx))

        # Pretend the last absence-safe walk started after this transaction's date.
        db.execute("DROP TRIGGER scopes_close_once")
        db.execute(
            "UPDATE scopes SET min_date = '2026-02-01' WHERE run_id = ? AND kind = 'transactions'"
            " AND parent = ?",
            (run_id, ACCOUNT_A1),
        )

        self.assertFalse(is_covered(db, tx))
        self.assertTrue(is_covered(db, document_id(db, "accounts", ACCOUNT_A1)))

    def test_nothing_is_covered_before_any_projection(self) -> None:
        db = self.harness.connection
        self.assertEqual(projected_run(db), 0)
        self.assertFalse(is_covered(db, 1))


if __name__ == "__main__":
    unittest.main()
