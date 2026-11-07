"""The README's caixinha yield query: the happy path, the timing rule that keeps
a transfer from being counted twice, and every reason it can answer with."""

from __future__ import annotations

import unittest
from typing import Any

from tests.fixtures.hand_built import HandBuilt
from tests.fixtures.readme import readme_query

ACC = "acc-0001"
ITEM = "item-000a"
D = {1: "2026-01-05", 2: "2026-01-06", 3: "2026-01-07"}
SNAPSHOT = {
    1: "2026-01-05T18:17:00.000Z",
    2: "2026-01-06T18:17:00.000Z",
    3: "2026-01-07T18:17:00.000Z",
}
EARLY = "2025-12-01T00:00:00+00:00"


def reserve(rid: str | None, name: str | None, amount: float, currency: str = "BRL") -> dict:
    entry: dict[str, Any] = {
        "availableAmounts": [
            {
                "amount": amount,
                "currencyCode": currency,
                "remuneration": {"indexer": "CDI", "postFixedIndexerPercentage": 1.2},
            }
        ]
    }
    if rid is not None:
        entry["identification"] = rid
    if name is not None:
        entry["name"] = name
    return entry


def account(reserves: list[dict], updated: str) -> dict:
    return {
        "id": ACC,
        "type": "BANK",
        "name": "checking",
        "currencyCode": "BRL",
        "balance": 1.0,
        "updatedAt": updated,
        "bankData": {"reservedBalances": reserves},
    }


def transfer(
    tid: str, date: str, amount: float, description: str, status: str = "POSTED", **extra
) -> dict:
    return {
        "id": tid,
        "date": date,
        "amount": amount,
        "currencyCode": "BRL",
        "description": description,
        "status": status,
        "operationType": "TRANSFERENCIA_SALDO_RESERVADO",
        "updatedAt": date,
        **extra,
    }


class YieldLedger:
    """One account, one run per day: reserves and the day's transactions."""

    def __init__(self) -> None:
        self.ledger = HandBuilt()

    def close(self) -> None:
        self.ledger.close()

    def day(
        self,
        n: int,
        reserves: list[dict] | None,
        transactions: list[dict] = (),
        min_instant: str | None = EARLY,
        walk: str = "complete",
        outcome: str = "complete",
        updated: str | None = None,
    ) -> int:
        run = self.ledger.run(outcome=outcome, at=f"{D[n]}T21:00:00+00:00")
        if reserves is not None:
            self.ledger.sight(
                run,
                self.ledger.scope(run, "accounts", ITEM),
                account(reserves, updated or SNAPSHOT[n]),
            )
        scope = self.ledger.scope(
            run, "transactions", ACC, outcome=walk, min_date="2025-12-01", min_instant=min_instant
        )
        for body in transactions:
            self.ledger.sight(run, scope, body)
        self.ledger.project(run)
        return run

    def yield_rows(self, d1: str, d2: str) -> list[tuple]:
        return self.ledger.db.execute(
            readme_query("caixinha_yield"), {"account_id": ACC, "d1": d1, "d2": d2}
        ).fetchall()


class YieldCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = YieldLedger()
        self.addCleanup(self.fixture.close)

    def rows(self, d1: str = D[1], d2: str = D[2]) -> dict[str | None, tuple]:
        return {
            row[0] if row[0] is not None else row[1]: row for row in self.fixture.yield_rows(d1, d2)
        }


class HappyPathTest(YieldCase):
    def test_deposit_and_withdrawal_are_removed_from_the_balance_difference(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0), reserve("R2", "Casa", 50.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 160.0), reserve("R2", "Casa", 30.0)],
            [
                transfer("t-dep", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem"),
                transfer("t-wd", "2026-01-06T11:00:00.000Z", 20.0, "Dinheiro retirado Casa"),
            ],
        )

        rows = self.rows()

        self.assertEqual(rows["R1"], ("R1", "Viagem", 10000, 16000, -5000, 1000, None))
        self.assertEqual(rows["R2"], ("R2", "Casa", 5000, 3000, 2000, 0, None))

    def test_transfers_before_the_snapshot_timestamp_count_once(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        five = [
            transfer(
                f"t-{i}",
                f"2026-01-06T17:3{4 + i // 3}:{(i * 20) % 60:02d}.000Z",
                -10.0,
                "Dinheiro reservado Viagem",
            )
            for i in range(5)
        ]
        after = transfer("t-late", "2026-01-06T18:30:00.000Z", -10.0, "Dinheiro reservado Viagem")
        # Already in prev's balance: dated before prev's snapshot, so outside the window.
        before = transfer("t-early", "2026-01-05T17:00:00.000Z", -10.0, "Dinheiro reservado Viagem")
        self.fixture.day(2, [reserve("R1", "Viagem", 150.0)], [before, *five, after])

        rows = self.rows()

        self.assertEqual(rows["R1"][4], -5000)
        self.assertEqual(rows["R1"][5], 0)

    def test_a_reserve_opened_between_the_cutoffs_starts_at_zero(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 100.0), reserve("R3", "Nova", 40.0)],
            [transfer("t-new", "2026-01-06T10:00:00.000Z", -40.0, "Dinheiro reservado Nova")],
        )

        rows = self.rows()

        self.assertEqual(rows["R3"], ("R3", "Nova", None, 4000, -4000, 0, None))

    def test_a_reserve_closed_between_the_cutoffs_has_no_yield(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0), reserve("R2", "Casa", 50.0)])
        self.fixture.day(2, [reserve("R1", "Viagem", 101.0)])

        rows = self.rows()

        self.assertEqual(
            rows["R2"], ("R2", "Casa", 5000, None, None, None, "closed or missing at D2")
        )
        self.assertEqual(rows["R1"][5], 100)

    def test_prev_from_a_migrated_run_is_accepted(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)], outcome="migrated")
        self.fixture.day(2, [reserve("R1", "Viagem", 101.0)])

        self.assertEqual(self.rows()["R1"][5], 100)

    def test_a_cutoff_without_a_qualifying_observation_returns_no_row(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(2, [reserve("R1", "Viagem", 101.0)])

        self.assertEqual(self.fixture.yield_rows("2025-12-31", D[2]), [])
        self.assertEqual(self.fixture.yield_rows(D[2], D[2]), [])

    def test_a_transfer_edited_after_d2_does_not_change_the_answer_for_d2(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 150.0)],
            [transfer("t-dep", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem")],
        )
        before = self.rows()
        self.fixture.day(
            3,
            [reserve("R1", "Viagem", 150.0)],
            [transfer("t-dep", "2026-01-06T10:00:00.000Z", -99.0, "Dinheiro reservado Viagem")],
        )

        self.assertEqual(self.rows(), before)
        self.assertEqual(before["R1"][5], 0)


class ReasonTest(YieldCase):
    def one_reserve_with(
        self, transfers: list[dict], curr_amount: float = 150.0, **day2: Any
    ) -> dict:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(2, [reserve("R1", "Viagem", curr_amount)], transfers, **day2)
        return self.rows()

    def test_a_rename_visible_only_in_an_intermediate_observation(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(2, [reserve("R1", "Ferias", 100.0)])
        self.fixture.day(
            3,
            [reserve("R1", "Viagem", 150.0)],
            [transfer("t-dep", "2026-01-07T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem")],
        )

        self.assertEqual(self.rows(D[1], D[3])["R1"][6], "name changed")

    def test_two_reserves_sharing_a_name(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 150.0), reserve("R9", "Viagem", 5.0)],
            [transfer("t-dep", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem")],
        )

        rows = self.fixture.yield_rows(D[1], D[2])

        unmatched = [row for row in rows if row[0] is None]
        self.assertEqual(unmatched, [(None, "Viagem", None, None, -5000, None, "name reused")])
        self.assertIsNone(next(row for row in rows if row[0] == "R1")[6])

    def test_an_unstable_entry_is_listed_on_its_own(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [
                reserve("R1", "Viagem", 101.0),
                reserve("dup", "A", 1.0),
                reserve("dup", "B", 2.0),
                reserve("", None, 3.0),
            ],
        )

        rows = self.fixture.yield_rows(D[1], D[2])

        self.assertEqual(
            sorted(row for row in rows if row[6] == "unstable"),
            [
                ("", None, None, 300, None, None, "unstable"),
                ("dup", "A", None, 100, None, None, "unstable"),
                ("dup", "B", None, 200, None, None, "unstable"),
            ],
        )
        self.assertEqual(next(row for row in rows if row[0] == "R1")[5], 100)

    def test_a_reserve_stable_at_one_cutoff_and_duplicated_at_the_other(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(2, [reserve("R1", "Viagem", 101.0), reserve("R1", "Outra", 1.0)])

        rows = self.fixture.yield_rows(D[1], D[2])

        self.assertEqual([row[6] for row in rows], ["unstable"] * len(rows))
        self.assertIn(("R1", "Viagem", 10000, None, None, None, "unstable"), rows)

    def test_a_transfer_whose_only_versions_are_ambiguous_is_left_out(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 150.0)],
            [
                transfer("t-dep", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem"),
                transfer("t-dep", "2026-01-06T10:00:00.000Z", -500.0, "Dinheiro reservado Viagem"),
            ],
        )

        rows = self.rows()

        self.assertIsNone(rows["R1"][4])
        self.assertEqual(rows["R1"][5], 5000)

    def test_an_ambiguous_account_observation_is_not_a_cutoff(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        run2 = self.fixture.ledger.run(at=f"{D[2]}T21:00:00+00:00")
        scope = self.fixture.ledger.scope(run2, "accounts", ITEM)
        self.fixture.ledger.sight(
            run2, scope, account([reserve("R1", "Viagem", 150.0)], SNAPSHOT[2])
        )
        self.fixture.ledger.sight(
            run2, scope, account([reserve("R1", "Viagem", 151.0)], SNAPSHOT[2])
        )
        self.fixture.ledger.project(run2)
        self.fixture.day(3, [reserve("R1", "Viagem", 160.0)])

        self.assertEqual(self.fixture.yield_rows(D[1], D[2]), [])
        self.assertEqual(self.rows(D[1], D[3])["R1"][5], 6000)

    def test_a_transfer_removed_before_d2(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 150.0)],
            [transfer("t-dep", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem")],
        )
        # Day 3: the absence-safe walk no longer returns the transfer.
        self.fixture.day(3, [reserve("R1", "Viagem", 150.0)], updated="2026-01-07T18:17:00.000Z")

        self.assertEqual(self.rows(D[1], D[3])["R1"][6], "transfer removed")
        self.assertEqual(self.rows(D[1], D[2])["R1"][6], None)

    def test_a_pending_transfer(self) -> None:
        rows = self.one_reserve_with(
            [
                transfer(
                    "t-dep",
                    "2026-01-06T10:00:00.000Z",
                    -50.0,
                    "Dinheiro reservado Viagem",
                    status="PENDING",
                )
            ]
        )
        self.assertEqual(rows["R1"][6], "transfer pending")

    def test_a_transfer_in_a_pending_posted_pair(self) -> None:
        rows = self.one_reserve_with(
            [
                transfer(
                    "t-posted", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Viagem"
                ),
                transfer(
                    "t-pending",
                    "2026-01-06T13:00:00.000Z",
                    -50.0,
                    "Dinheiro reservado Viagem",
                    status="PENDING",
                ),
            ]
        )
        self.assertEqual(rows["R1"][6], "transfer unresolved")

    def test_a_transfer_with_the_wrong_sign(self) -> None:
        rows = self.one_reserve_with(
            [transfer("t-dep", "2026-01-06T10:00:00.000Z", 50.0, "Dinheiro reservado Viagem")]
        )
        self.assertEqual(rows["R1"][6], "transfer sign")

    def test_a_walk_that_does_not_reach_back_to_prev(self) -> None:
        rows = self.one_reserve_with([], min_instant="2026-01-06T00:00:00+00:00")
        self.assertEqual(rows["R1"][6], "coverage gap")

    def test_a_walk_that_is_not_absence_safe(self) -> None:
        rows = self.one_reserve_with([], walk="incomplete")
        self.assertEqual(rows["R1"][6], "coverage gap")

    def test_a_transfer_naming_no_reserve(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0)])
        self.fixture.day(
            2,
            [reserve("R1", "Viagem", 100.0)],
            [transfer("t-x", "2026-01-06T10:00:00.000Z", -50.0, "Dinheiro reservado Nada")],
        )

        rows = self.fixture.yield_rows(D[1], D[2])

        self.assertIn((None, "Nada", None, None, -5000, None, "unmatched transfer"), rows)
        self.assertEqual(next(row for row in rows if row[0] == "R1")[5], 0)

    def test_a_foreign_currency_and_a_sub_cent_amount(self) -> None:
        self.fixture.day(1, [reserve("R1", "Viagem", 100.0), reserve("R2", "Casa", 50.0)])
        self.fixture.day(
            2, [reserve("R1", "Viagem", 100.0, currency="USD"), reserve("R2", "Casa", 50.001)]
        )

        rows = self.rows()

        self.assertEqual(rows["R1"][6], "currency")
        self.assertEqual(rows["R2"][6], "amount")


if __name__ == "__main__":
    unittest.main()
