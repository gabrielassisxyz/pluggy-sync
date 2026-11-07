"""The nine current-state tables: a pure function of the ledger, equal to a
rebuild after every run, including runs a crash interrupted."""

from __future__ import annotations

import sqlite3
import unittest

from pluggysync.coverage import projected_run
from pluggysync.projections import PROJECTION_NAMES
from pluggysync.rebuild import rebuild
from pluggysync.walk import BASE
from tests.fakes import ok
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

ITEMS = [ITEM_A, ITEM_B]


def tables_snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY id").fetchall()
        for name in PROJECTION_NAMES
    }


class Crash(BaseException):
    pass


def crash_when(point: str, **match: object):
    def hook(name: str, **context: object) -> None:
        if name == point and all(context.get(k) == v for k, v in match.items()):
            raise Crash(point)

    return hook


class ProjectionEqualsRebuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)

    def assert_equals_rebuild(self) -> None:
        db = self.harness.connection
        incremental = tables_snapshot(db)
        checkpoint = projected_run(db)
        rebuild(db)
        self.assertEqual(tables_snapshot(db), incremental)
        self.assertEqual(projected_run(db), checkpoint)

    def test_every_table_equals_the_rebuild_after_each_run_of_a_changing_fixture(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        run1 = self.harness.run(first, ITEMS).execute()
        self.assert_equals_rebuild()

        second = self.harness.transport()
        script_two_items(second)
        changed = tx_doc("tx-a1-0001", ACCOUNT_A1)
        changed["amount"] = -99.0
        second.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second.answer(f"/v2/transactions{cursor_query(ACCOUNT_A1)}", ok(cursor_page([changed])))
        second.script[f"{BASE}/accounts?itemId={ITEM_A}&{PAGE_1}"].clear()
        second.answer(
            f"/accounts?itemId={ITEM_A}&{PAGE_1}",
            ok(paged_page([account_doc(ACCOUNT_A1, ITEM_A)])),
        )
        run2 = self.harness.run(second, ITEMS).execute()
        self.assert_equals_rebuild()

        db = self.harness.connection
        self.assertEqual(projected_run(db), run2)
        row = db.execute(
            "SELECT amount, first_seen_run, last_seen_run, state FROM transactions WHERE id = ?",
            ("tx-a1-0001",),
        ).fetchone()
        self.assertEqual(row, (-99.0, run1, run2, "present"))
        gone = db.execute(
            "SELECT state, last_seen_run FROM accounts WHERE id = ?", (ACCOUNT_A2,)
        ).fetchone()
        self.assertEqual(gone, ("removed", run1))
        self.assertEqual(
            db.execute("SELECT state FROM bills WHERE account_id = ?", (ACCOUNT_A2,)).fetchone()[0],
            "parent_gone",
        )

    def test_crash_after_a_scope_close_is_projected_by_recovery_and_equals_the_rebuild(
        self,
    ) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.harness.run(first, ITEMS).execute()
        second = self.harness.transport()
        script_two_items(second)
        with self.assertRaises(Crash):
            self.harness.run(
                second,
                ITEMS,
                crash_at=crash_when("after_scope_close", kind="accounts", parent=ITEM_A),
            ).execute()
        self.harness.reopen()

        swept = self.harness.run(self.harness.transport(), ITEMS).sweep()

        self.assertEqual(len(swept), 1)
        self.assertEqual(projected_run(self.harness.connection), swept[0])
        self.assert_equals_rebuild()

    def test_crash_during_run_close_leaves_the_run_running_and_recovery_projects_it(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        with self.assertRaises(Crash):
            self.harness.run(transport, ITEMS, crash_at=crash_when("during_run_close")).execute()
        self.harness.reopen()
        db = self.harness.connection
        self.assertEqual(projected_run(db), 0)
        self.assertEqual(db.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)

        swept = self.harness.run(self.harness.transport(), ITEMS).sweep()

        self.assertEqual(len(swept), 1)
        self.assertEqual(self.harness.run_row(swept[0])["outcome"], "interrupted")
        self.assertGreater(db.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)
        self.assert_equals_rebuild()


class ProjectionColumnsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        transport = self.harness.transport()
        script_two_items(transport)
        self.run_id = self.harness.run(transport, ITEMS).execute()
        self.db = self.harness.connection

    def test_parent_columns_come_from_the_sighting_not_the_body(self) -> None:
        bill = self.db.execute("SELECT account_id, raw FROM bills").fetchone()
        self.assertEqual(bill[0], ACCOUNT_A2)
        self.assertNotIn("accountId", bill[1])
        movement = self.db.execute("SELECT investment_id FROM investment_transactions").fetchone()
        self.assertEqual(movement[0], "investment-00a1")

    def test_history_columns_and_raw_are_filled(self) -> None:
        row = self.db.execute(
            "SELECT raw, fetched_at, first_seen_run, last_seen_run, state, ambiguous_run"
            " FROM accounts WHERE id = ?",
            (ACCOUNT_A1,),
        ).fetchone()
        self.assertTrue(row[0].startswith('{"balance":'))
        self.assertEqual(
            (row[2], row[3], row[4], row[5]), (self.run_id, self.run_id, "present", None)
        )
        observed_at = self.db.execute(
            "SELECT o.observed_at FROM observations o JOIN documents d ON d.id = o.document_id"
            " WHERE d.provider_id = ?",
            (ACCOUNT_A1,),
        ).fetchone()[0]
        self.assertEqual(row[1], observed_at)

    def test_every_legacy_query_shape_still_works(self) -> None:
        self.assertEqual(self.db.execute("SELECT count(*) FROM categories").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("SELECT connector FROM items WHERE id = ?", (ITEM_A,)).fetchone()[0],
            "Synthetic Bank",
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM transactions WHERE account_id = ?", (ACCOUNT_A2,)
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.db.execute("SELECT item_id FROM consents").fetchall(), [(ITEM_A,), (ITEM_B,)]
        )


if __name__ == "__main__":
    unittest.main()
