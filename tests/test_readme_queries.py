"""Every example query in the README runs against a synthetic ledger and
answers what the prose beside it promises."""

from __future__ import annotations

import unittest

from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    INVESTMENT_A1,
    ITEM_A,
    ITEM_B,
    LedgerHarness,
    script_two_items,
)
from tests.fixtures.readme import readme_queries, readme_query
from tests.test_views import account_with_reserves, amount, reserve

EXPECTED_QUERIES = {
    "balance_as_of",
    "reserves_as_of",
    "cdb_history",
    "transaction_history",
    "clean_list",
    "unresolved_list",
    "not_covered_list",
    "pending_pairs",
}


class ReadmeInventoryTest(unittest.TestCase):
    def test_the_readme_carries_every_documented_query(self) -> None:
        self.assertEqual(set(readme_queries()), EXPECTED_QUERIES)


class ReadmeOnTheHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        transport = self.harness.transport()
        script_two_items(transport)
        self.run_id = self.harness.run(transport, [ITEM_A, ITEM_B]).execute()
        self.db = self.harness.connection
        self.day = self.harness.run_row(self.run_id)["finished_day"]

    def test_balance_as_of_is_covered_on_the_run_day_and_no_run_before_it(self) -> None:
        on_the_day = self.db.execute(
            readme_query("balance_as_of"), {"account_id": ACCOUNT_A1, "day": self.day}
        ).fetchone()
        self.assertEqual((on_the_day[4], on_the_day[-1]), (100.0, "covered"))
        before = self.db.execute(
            readme_query("balance_as_of"), {"account_id": ACCOUNT_A1, "day": "2025-12-31"}
        ).fetchone()
        self.assertIsNone(before)

    def test_cdb_and_transaction_histories_answer_for_the_fixture(self) -> None:
        cdb = self.db.execute(
            readme_query("cdb_history"), {"investment_id": INVESTMENT_A1}
        ).fetchall()
        self.assertEqual(len(cdb), 1)
        history = self.db.execute(
            readme_query("transaction_history"), {"transaction_id": "tx-a1-0001"}
        ).fetchall()
        self.assertEqual([row[-2] for row in history], ["appeared"])

    def test_the_lists_partition_the_present_transactions(self) -> None:
        clean = self.db.execute(readme_query("clean_list")).fetchall()
        unresolved = self.db.execute(readme_query("unresolved_list")).fetchall()
        not_covered = self.db.execute(readme_query("not_covered_list")).fetchall()
        self.assertEqual(len(clean) + len(unresolved) + len(not_covered), 4)
        self.assertEqual(len(clean), 4)
        self.assertEqual(self.db.execute(readme_query("pending_pairs")).fetchall(), [])


class ReadmeReservesTest(unittest.TestCase):
    def test_reserves_as_of_follows_the_account_observation_not_the_last_reserve(self) -> None:
        ledger = HandBuilt()
        self.addCleanup(ledger.close)
        run1 = ledger.run()
        ledger.sight(
            run1,
            ledger.scope(run1, "accounts", ITEM_A),
            account_with_reserves([reserve("r-1", "Viagem", [amount(500.0)])]),
        )
        ledger.project(run1)
        day1 = ledger.db.execute("SELECT finished_day FROM runs WHERE id = ?", (run1,)).fetchone()[
            0
        ]
        run2 = ledger.run()
        ledger.sight(run2, ledger.scope(run2, "accounts", ITEM_A), account_with_reserves([]))
        ledger.project(run2)
        day2 = ledger.db.execute("SELECT finished_day FROM runs WHERE id = ?", (run2,)).fetchone()[
            0
        ]

        at_day1 = ledger.db.execute(
            readme_query("reserves_as_of"), {"account_id": "acc-0001", "day": day1}
        ).fetchall()
        at_day2 = ledger.db.execute(
            readme_query("reserves_as_of"), {"account_id": "acc-0001", "day": day2}
        ).fetchall()

        self.assertEqual([(row[3], row[7]) for row in at_day1], [("r-1", 500.0)])
        self.assertEqual([(row[0], row[3]) for row in at_day2], [(day2, None)])
        # The coverage column of the balance query says the account walk vouched for the day.
        balance = ledger.db.execute(
            readme_query("balance_as_of"), {"account_id": "acc-0001", "day": day2}
        ).fetchone()
        self.assertEqual(balance[-1], "covered")


if __name__ == "__main__":
    unittest.main()
