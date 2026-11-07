"""A run that dies is closed honestly on the next start, by what it archived."""

from __future__ import annotations

import unittest

from pluggysync.archive import archived_responses
from pluggysync.replay import replay_walk
from pluggysync.walk import BASE
from tests.fakes import ok
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ACCOUNT_A2,
    ACCOUNT_B1,
    INVESTMENT_A1,
    ITEM_A,
    ITEM_B,
    LedgerHarness,
    script_two_items,
    tx_doc,
)
from tests.fixtures.pages import cursor_page, cursor_query

ITEMS = [ITEM_A, ITEM_B]


class SimulatedCrash(BaseException):
    """Not an Exception: the run's failure handling must not see it, exactly as
    it would not see SIGKILL or a power loss."""


def crash_when(name: str, **match: object):
    def hook(point: str, **context: object) -> None:
        if point == name and all(context.get(key) == value for key, value in match.items()):
            raise SimulatedCrash(point)

    return hook


class RecoveryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)

    def crash_then_sweep(self, hook, transport=None) -> int:
        transport = transport or self.harness.transport()
        if not transport.script:
            script_two_items(transport)
        with self.assertRaises(SimulatedCrash):
            self.harness.run(transport, ITEMS, crash_at=hook).execute()
        self.harness.reopen()
        swept = self.harness.run(self.harness.transport(), ITEMS).sweep()
        self.assertEqual(len(swept), 1)
        return swept[0]

    def assertSettled(self, run_id: int) -> None:
        row = self.harness.run_row(run_id)
        self.assertEqual(row["outcome"], "interrupted")
        self.assertIn("interrupted", row["note"])
        self.assertIsNotNone(row["finished_at"])
        self.assertIsNotNone(row["finished_day"])
        self.assertEqual(
            self.harness.count(
                "SELECT count(*) FROM scopes WHERE run_id=? AND outcome='pending'", run_id
            ),
            0,
        )
        self.assertEqual(
            row["api_calls"],
            self.harness.count("SELECT count(*) FROM responses WHERE run_id=?", run_id),
        )


class SweepTest(RecoveryCase):
    def test_crash_before_any_response_closes_everything_from_recovery_time(self) -> None:
        run_id = self.crash_then_sweep(crash_when("before_first_request"))

        self.assertSettled(run_id)
        scopes = self.harness.scopes(run_id)
        self.assertEqual(len(scopes), 11)
        for row in scopes.values():
            self.assertEqual(row["outcome"], "incomplete")
            self.assertTrue(row["error"].startswith("swept: "), row["error"])
        self.assertEqual(self.harness.run_row(run_id)["api_calls"], 0)

    def test_crash_after_an_archive_commit_before_derivation(self) -> None:
        run_id = self.crash_then_sweep(
            crash_when(
                "after_archive_commit", url=f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A2)}"
            )
        )

        self.assertSettled(run_id)
        scopes = self.harness.scopes(run_id)
        # The terminal page was archived, so the walk is proven complete.
        a2 = scopes[("transactions", ACCOUNT_A2)]
        self.assertEqual((a2["outcome"], a2["infer_absence"], a2["rows"]), ("complete", 1, 2))
        self.assertEqual(
            self.harness.count("SELECT count(*) FROM observations WHERE scope_id=?", a2["id"]), 2
        )
        self.assertEqual(scopes[("transactions", ACCOUNT_A1)]["outcome"], "complete")

    def test_crash_after_a_valid_parent_page_skips_the_implied_children(self) -> None:
        run_id = self.crash_then_sweep(
            crash_when("after_archive_commit", url=f"{BASE}/items/{ITEM_B}")
        )

        self.assertSettled(run_id)
        scopes = self.harness.scopes(run_id)
        self.assertEqual(scopes[("items", ITEM_B)]["outcome"], "complete")
        self.assertEqual(scopes[("items", ITEM_B)]["rows"], 1)
        for kind in ("consents", "identities", "investments", "accounts"):
            self.assertEqual(scopes[(kind, ITEM_B)]["outcome"], "incomplete")
            self.assertTrue(scopes[(kind, ITEM_B)]["error"].startswith("swept: "))

    def test_crash_after_a_terminal_page_before_scope_close(self) -> None:
        run_id = self.crash_then_sweep(
            crash_when("before_scope_close", kind="accounts", parent=ITEM_A)
        )

        self.assertSettled(run_id)
        scopes = self.harness.scopes(run_id)
        accounts = scopes[("accounts", ITEM_A)]
        self.assertEqual(
            (accounts["outcome"], accounts["infer_absence"], accounts["rows"]), ("complete", 1, 2)
        )
        # Children implied by the recovered page exist, closed skipped and owed.
        for key in (
            ("transactions", ACCOUNT_A1),
            ("transactions", ACCOUNT_A2),
            ("bills", ACCOUNT_A2),
        ):
            self.assertEqual((scopes[key]["outcome"], scopes[key]["expected"]), ("skipped", 1), key)
            self.assertEqual(scopes[key]["closed_at"], accounts["closed_at"])
        self.assertNotIn(("bills", ACCOUNT_A1), scopes)
        self.assertNotIn(("transactions", ACCOUNT_B1), scopes)  # B's accounts walk never happened

    def test_crash_after_a_scope_close(self) -> None:
        run_id = self.crash_then_sweep(
            crash_when("after_scope_close", kind="investments", parent=ITEM_A)
        )

        self.assertSettled(run_id)
        scopes = self.harness.scopes(run_id)
        self.assertEqual(scopes[("investments", ITEM_A)]["outcome"], "complete")
        self.assertEqual(scopes[("accounts", ITEM_A)]["outcome"], "incomplete")
        child = scopes[("investment_transactions", INVESTMENT_A1)]
        self.assertEqual((child["outcome"], child["expected"]), ("skipped", 1))

    def test_crash_mid_cursor_walk_keeps_page_1_and_closes_incomplete(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second = cursor_query(ACCOUNT_A1, after="c1")
        transport.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
            ok(cursor_page([tx_doc("tx-a1-0001", ACCOUNT_A1)], second)),
        )
        transport.answer(
            f"/v2/transactions{second}", ok(cursor_page([tx_doc("tx-a1-0002", ACCOUNT_A1)]))
        )

        run_id = self.crash_then_sweep(
            crash_when(
                "after_archive_commit", url=f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"
            ),
            transport,
        )

        self.assertSettled(run_id)
        scope = self.harness.scopes(run_id)[("transactions", ACCOUNT_A1)]
        self.assertEqual(
            (scope["outcome"], scope["infer_absence"], scope["rows"]), ("incomplete", 0, 1)
        )
        self.assertTrue(scope["error"].startswith("swept: "))
        self.assertIn("no further attempt archived", scope["error"])

    def test_a_clean_database_has_nothing_to_sweep(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        self.harness.run(transport, ITEMS).execute()

        self.assertEqual(self.harness.run(self.harness.transport(), ITEMS).sweep(), [])


class ReplayTest(RecoveryCase):
    def test_replay_reaches_the_live_verdict_from_the_archive(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        run_id = self.harness.run(transport, ITEMS).execute()

        for (kind, parent), row in self.harness.scopes(run_id).items():
            if row["outcome"] == "skipped":
                continue
            with self.subTest(kind=kind, parent=parent):
                result = replay_walk(
                    kind, parent, archived_responses(self.harness.connection, row["id"])
                )
                self.assertEqual(result.outcome, row["outcome"])
                self.assertEqual(result.status, row["status"])
                self.assertEqual(len(result.documents), row["rows"])


class ReplayMismatchTest(RecoveryCase):
    def test_an_archived_attempt_for_another_url_is_not_served_as_this_walk(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        run_id = self.harness.run(transport, ITEMS).execute()
        scopes = self.harness.scopes(run_id)
        foreign = archived_responses(
            self.harness.connection, scopes[("transactions", ACCOUNT_B1)]["id"]
        )

        result = replay_walk("transactions", ACCOUNT_A1, foreign)

        self.assertEqual(result.outcome, "incomplete")
        self.assertEqual(result.pages, [])
        self.assertIn("no further attempt archived", result.error or "")


if __name__ == "__main__":
    unittest.main()
