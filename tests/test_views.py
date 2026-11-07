"""Recorded expected output of each history view on fixed, hand-built sightings."""

from __future__ import annotations

import unittest

from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.ledger_run import ITEM_A, ITEM_B, LedgerHarness, script_two_items
from tests.fixtures.readme import readme_query

ACC = "acc-0001"
OTHER = "acc-0002"


def reserve(identification: str | None, name: str | None, amounts: list[dict] | None) -> dict:
    entry: dict = {}
    if identification is not None:
        entry["identification"] = identification
    if name is not None:
        entry["name"] = name
    if amounts is not None:
        entry["availableAmounts"] = amounts
    return entry


def amount(value: float, indexer: str = "CDI", ratio: float = 1.2) -> dict:
    return {
        "amount": value,
        "currencyCode": "BRL",
        "remuneration": {"indexer": indexer, "postFixedIndexerPercentage": ratio},
    }


def account_with_reserves(
    reserves, account_id: str = ACC, updated: str = "2026-01-01T18:00:00.000Z"
) -> dict:
    return {
        "id": account_id,
        "type": "BANK",
        "name": "checking",
        "currencyCode": "BRL",
        "balance": 100.0,
        "updatedAt": updated,
        "bankData": {"closingBalance": 100.0, "reservedBalances": reserves},
    }


def tx(tx_id: str, date: str = "2026-01-10T12:00:00.000Z", status: str = "POSTED", **extra) -> dict:
    return {
        "id": tx_id,
        "date": date,
        "amount": -10.0,
        "currencyCode": "BRL",
        "description": "purchase",
        "status": status,
        "updatedAt": date,
        **extra,
    }


class ViewCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = HandBuilt()
        self.addCleanup(self.ledger.close)
        self.db = self.ledger.db

    def rows(self, sql: str, *parameters: object) -> list[tuple]:
        return self.db.execute(sql, parameters).fetchall()


class ReservedBalanceHistoryTest(ViewCase):
    def test_one_row_per_entry_with_the_documented_values(self) -> None:
        run = self.ledger.run()
        scope = self.ledger.scope(run, "accounts", ITEM_A)
        reserves = [
            reserve("r-1", "Viagem", [amount(500.0)]),
            reserve("r-2", None, [amount(0.0)]),
            reserve("dup", "A", [amount(1.0)]),
            reserve("dup", "B", [amount(2.0)]),
            reserve("r-3", "Vazia", []),
            reserve("r-4", "Dupla", [amount(1.0), amount(2.0)]),
            reserve("r-5", "Poupanca", [amount(10.0, indexer="TR", ratio=0.7)]),
            reserve("", "Sem id", [amount(3.0)]),
        ]
        self.ledger.sight(run, scope, account_with_reserves(reserves))
        self.ledger.project(run)

        rows = self.rows(
            "SELECT position, reserve_id, stable_identity, reserve_name, amount, indexer,"
            " indexer_ratio, cdi_percentage, amount_status FROM reserved_balance_history"
            " WHERE account_id = ? ORDER BY position",
            ACC,
        )

        self.assertEqual(
            rows,
            [
                (0, "r-1", 1, "Viagem", 500.0, "CDI", 1.2, 120.0, "ok"),
                (1, "r-2", 1, None, 0.0, "CDI", 1.2, 120.0, "ok"),
                (2, "dup", 0, "A", 1.0, "CDI", 1.2, 120.0, "ok"),
                (3, "dup", 0, "B", 2.0, "CDI", 1.2, 120.0, "ok"),
                (4, "r-3", 1, "Vazia", None, None, None, None, "no availableAmounts element"),
                (5, "r-4", 1, "Dupla", None, None, None, None, "several availableAmounts elements"),
                (6, "r-5", 1, "Poupanca", 10.0, "TR", 0.7, None, "ok"),
                (7, "", 0, "Sem id", 3.0, "CDI", 1.2, 120.0, "ok"),
            ],
        )

    def test_null_absent_and_empty_arrays_yield_no_rows(self) -> None:
        run = self.ledger.run()
        scope = self.ledger.scope(run, "accounts", ITEM_A)
        self.ledger.sight(run, scope, account_with_reserves(None, "acc-null"))
        self.ledger.sight(run, scope, account_with_reserves([], "acc-empty"))
        absent = account_with_reserves([], "acc-absent")
        del absent["bankData"]["reservedBalances"]
        self.ledger.sight(run, scope, absent)
        self.ledger.project(run)

        self.assertEqual(self.rows("SELECT count(*) FROM reserved_balance_history"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM account_balance_history"), [(3,)])

    def test_as_of_query_returns_no_reserve_once_the_array_emptied(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1,
            self.ledger.scope(run1, "accounts", ITEM_A),
            account_with_reserves([reserve("r-1", "Viagem", [amount(500.0)])]),
        )
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.sight(
            run2,
            self.ledger.scope(run2, "accounts", ITEM_A),
            account_with_reserves([], updated="2026-01-02T18:00:00.000Z"),
        )
        self.ledger.project(run2)
        day = self.db.execute("SELECT finished_day FROM runs WHERE id = ?", (run2,)).fetchone()[0]

        rows = self.rows(
            "SELECT a.day, r.reserve_id, r.amount FROM account_balance_history a"
            " LEFT JOIN reserved_balance_history r ON r.observation_id = a.observation_id"
            " WHERE a.account_id = ? AND a.day <= ? ORDER BY a.day DESC LIMIT 1",
            ACC,
            day,
        )

        self.assertEqual(rows, [(day, None, None)])


class TransactionVersionsTest(ViewCase):
    def walk(self, run_id: int, parent: str = ACC) -> int:
        return self.ledger.scope(run_id, "transactions", parent, min_date="2026-01-01")

    def versions(self, provider_id: str) -> list[tuple]:
        return self.rows(
            "SELECT run_id, account_id, events, ambiguous FROM transaction_versions"
            " WHERE provider_id = ? ORDER BY run_id, observed_at",
            provider_id,
        )

    def test_removed_then_reappeared_with_the_same_body_is_two_rows(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.walk(run2)
        self.ledger.project(run2)
        run3 = self.ledger.run()
        self.ledger.sight(run3, self.walk(run3), tx("t1"))
        self.ledger.project(run3)

        self.assertEqual(
            self.versions("t1"), [(run1, ACC, "appeared", 0), (run3, ACC, "reappeared", 0)]
        )

    def test_a_move_with_an_identical_body_is_two_rows(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(run1, self.walk(run1), tx("t1"))
        self.ledger.project(run1)
        run2 = self.ledger.run()
        self.ledger.sight(run2, self.walk(run2, OTHER), tx("t1"))
        self.ledger.project(run2)

        self.assertEqual(
            self.versions("t1"), [(run1, ACC, "appeared", 0), (run2, OTHER, "moved", 0)]
        )

    def test_two_bodies_in_one_run_are_two_rows_marked_ambiguous(self) -> None:
        run1 = self.ledger.run()
        scope = self.walk(run1)
        self.ledger.sight(run1, scope, tx("t1"))
        self.ledger.sight(run1, scope, tx("t1", amount=-20.0))
        self.ledger.project(run1)

        self.assertEqual(self.versions("t1"), [(run1, ACC, None, 1), (run1, ACC, None, 1)])

    def test_identical_daily_sightings_collapse_into_one_row(self) -> None:
        for _ in range(365):
            run = self.ledger.run(at=self.ledger.tick(24))
            self.ledger.sight(run, self.walk(run), tx("t1"))
            self.ledger.project(run)

        self.assertEqual(len(self.versions("t1")), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM observations"), [(365,)])


class PendingPostedCandidatesTest(ViewCase):
    def test_a_pending_and_its_posted_twin_are_paired_and_only_the_posted_one_is_clean(
        self,
    ) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1,
            self.ledger.scope(run1, "transactions", ACC, min_date="2025-09-01"),
            tx("pending-1", date="2026-09-08T15:00:00.000Z", status="PENDING"),
        )
        self.ledger.project(run1)
        run2 = self.ledger.run()
        scope = self.ledger.scope(run2, "transactions", ACC, min_date="2025-09-01")
        # The provider shows both for a while: the pending entry and its posted
        # twin, three hours apart on the same UTC day.
        self.ledger.sight(
            run2, scope, tx("pending-1", date="2026-09-08T15:00:00.000Z", status="PENDING")
        )
        self.ledger.sight(run2, scope, tx("posted-1", date="2026-09-08T18:00:00.000Z"))
        self.ledger.project(run2)

        pairs = self.rows(
            "SELECT pending_id, posted_id, pending_covered FROM pending_posted_candidates"
        )
        self.assertEqual(pairs, [("pending-1", "posted-1", 1)])
        self.assertEqual([row[0] for row in self.rows(CLEAN_LIST)], ["posted-1"])
        unresolved = self.rows(UNRESOLVED_LIST)
        self.assertEqual(
            [(row[0], row[5]) for row in unresolved], [("pending-1", "pending with a posted twin")]
        )

    def test_a_pending_whose_twin_replaced_it_is_removed_by_the_absence_safe_walk(self) -> None:
        run1 = self.ledger.run()
        self.ledger.sight(
            run1,
            self.ledger.scope(run1, "transactions", ACC, min_date="2025-09-01"),
            tx("pending-1", date="2026-09-08T15:00:00.000Z", status="PENDING"),
        )
        self.ledger.project(run1)
        run2 = self.ledger.run()
        scope = self.ledger.scope(run2, "transactions", ACC, min_date="2025-09-01")
        self.ledger.sight(run2, scope, tx("posted-1", date="2026-09-08T18:00:00.000Z"))
        self.ledger.project(run2)

        self.assertEqual(self.ledger.state("transactions", "pending-1")["state"], "removed")
        self.assertEqual(self.rows("SELECT count(*) FROM pending_posted_candidates"), [(0,)])
        self.assertEqual([row[0] for row in self.rows(CLEAN_LIST)], ["posted-1"])

    def test_a_covered_pending_without_a_twin_is_clean_and_an_uncovered_one_is_unresolved(
        self,
    ) -> None:
        run1 = self.ledger.run()
        scope = self.ledger.scope(run1, "transactions", ACC, min_date="2026-01-05")
        self.ledger.sight(
            run1, scope, tx("pending-covered", date="2026-01-10T12:00:00.000Z", status="PENDING")
        )
        self.ledger.sight(
            run1, scope, tx("pending-old", date="2026-01-02T12:00:00.000Z", status="PENDING")
        )
        self.ledger.sight(run1, scope, tx("posted-old", date="2026-01-02T13:00:00.000Z"))
        self.ledger.project(run1)

        self.assertEqual([row[0] for row in self.rows(CLEAN_LIST)], ["pending-covered"])
        self.assertEqual([row[0] for row in self.rows(UNRESOLVED_LIST)], ["pending-old"])
        self.assertEqual([row[0] for row in self.rows(NOT_COVERED_LIST)], ["posted-old"])


class PendingPostedGuardsTest(ViewCase):
    def seed(self, posted_date: str, posted_min_date: str = "2025-09-01") -> None:
        run1 = self.ledger.run()
        scope = self.ledger.scope(run1, "transactions", ACC, min_date=posted_min_date)
        self.ledger.sight(
            run1, scope, tx("pending-1", date="2026-09-08T15:00:00.000Z", status="PENDING")
        )
        self.ledger.sight(run1, scope, tx("posted-1", date=posted_date))
        self.ledger.project(run1)

    def test_a_posted_twin_on_another_utc_day_is_not_paired(self) -> None:
        self.seed(posted_date="2026-09-09T18:00:00.000Z")
        self.assertEqual(self.rows("SELECT count(*) FROM pending_posted_candidates"), [(0,)])

    def test_an_uncovered_posted_twin_is_not_paired(self) -> None:
        self.seed(posted_date="2026-09-08T18:00:00.000Z", posted_min_date="2026-10-01")
        self.assertEqual(self.rows("SELECT count(*) FROM pending_posted_candidates"), [(0,)])


class RunCoverageTest(ViewCase):
    def test_outcome_columns_partition_the_owed_walks(self) -> None:
        run = self.ledger.run(outcome="partial")
        self.ledger.scope(run, "categories", "")
        self.ledger.scope(run, "items", ITEM_A, outcome="incomplete")
        self.ledger.scope(run, "accounts", ITEM_A, outcome="skipped", infer_absence=0)
        self.ledger.scope(run, "investments", ITEM_A, outcome="unavailable", infer_absence=0)
        self.ledger.project(run)

        row = self.rows(
            "SELECT expected, complete, unavailable, incomplete, skipped, pending,"
            " ambiguous_documents FROM run_coverage WHERE run_id = ?",
            run,
        )
        self.assertEqual(row, [(4, 1, 1, 1, 1, 0, 0)])


class ConfigurationDropTest(unittest.TestCase):
    def test_an_item_dropped_from_configuration_leaves_its_documents_present_and_uncovered(
        self,
    ) -> None:
        harness = LedgerHarness()
        self.addCleanup(harness.close)
        first = harness.transport()
        script_two_items(first)
        harness.run(first, [ITEM_A, ITEM_B]).execute()
        second = harness.transport()
        script_two_items(second)
        harness.run(second, [ITEM_A]).execute()
        db = harness.connection

        rows = db.execute(
            "SELECT state, covered FROM document_coverage WHERE kind = 'transactions'"
            " AND provider_id = 'tx-b1-0001'"
        ).fetchall()
        self.assertEqual(rows, [("present", 0)])
        self.assertEqual(
            db.execute(
                "SELECT count(*) FROM events e JOIN documents d ON d.id = e.document_id"
                " WHERE d.provider_id = 'tx-b1-0001'"
            ).fetchone()[0],
            1,  # only its `appeared`
        )
        self.assertEqual(
            [r[0] for r in db.execute(NOT_COVERED_LIST) if r[0] == "tx-b1-0001"], ["tx-b1-0001"]
        )


CLEAN_LIST = readme_query("clean_list")
UNRESOLVED_LIST = readme_query("unresolved_list")
NOT_COVERED_LIST = readme_query("not_covered_list")


if __name__ == "__main__":
    unittest.main()
