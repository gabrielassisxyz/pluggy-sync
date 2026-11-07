"""`--rebuild` is one transaction: an interruption anywhere leaves the previous
tables, indexes and views usable, and a retry succeeds."""

from __future__ import annotations

import sqlite3
import unittest

from pluggysync.coverage import projected_run
from pluggysync.projections import PROJECTION_NAMES
from pluggysync.rebuild import rebuild
from pluggysync.views import VIEW_NAMES
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, LedgerHarness, script_two_items


def everything_derived(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    names = (*PROJECTION_NAMES, "events", "ambiguities", "document_state", "projection_state")
    return {name: connection.execute(f"SELECT * FROM {name}").fetchall() for name in names}


class Interrupted(Exception):
    pass


class RebuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        transport = self.harness.transport()
        script_two_items(transport)
        self.run_id = self.harness.run(transport, [ITEM_A, ITEM_B]).execute()
        self.db = self.harness.connection

    def test_an_interruption_at_each_point_leaves_the_previous_state_usable(self) -> None:
        before = everything_derived(self.db)
        for point in ("before_rebuild", "after_clearing", "after_replay"):

            def interrupt(name: str, point: str = point, **context: object) -> None:
                if name == point:
                    raise Interrupted(point)

            with self.subTest(point=point), self.assertRaises(Interrupted):
                rebuild(self.db, crash_at=interrupt)
            self.assertFalse(self.db.in_transaction)
            self.assertEqual(everything_derived(self.db), before)
            for view in VIEW_NAMES:
                self.db.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()
            self.assertEqual(
                self.db.execute("PRAGMA index_list(transactions)").fetchall().__len__(), 3
            )

        self.assertEqual(rebuild(self.db), [self.run_id])
        self.assertEqual(everything_derived(self.db), before)

    def test_no_scratch_table_remains_after_success(self) -> None:
        rebuild(self.db)
        names = [
            name for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        self.assertFalse([n for n in names if n.startswith("new_") or n.startswith("sqlite_stat")])

    def test_rebuild_on_an_empty_ledger_leaves_no_checkpoint_row(self) -> None:
        empty = LedgerHarness()
        self.addCleanup(empty.close)

        self.assertEqual(rebuild(empty.connection), [])
        self.assertEqual(projected_run(empty.connection), 0)
        self.assertEqual(
            empty.connection.execute("SELECT count(*) FROM projection_state").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
