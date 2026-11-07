"""The ledger write path: planning, tiers, archive rows, derivation, skipped
children, ambiguity, and how a run closes."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import unittest
from contextlib import redirect_stderr

from pluggysync.derive import Scope, derive_page
from pluggysync.run import ConfigurationError, parse_item_ids, self_check
from pluggysync.walk import BASE, Page
from tests.fakes import ok, raw, refused, truncated, unreachable
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ACCOUNT_A2,
    ACCOUNT_B1,
    INVESTMENT_A1,
    ITEM_A,
    ITEM_B,
    PAGE_1,
    LedgerHarness,
    account_doc,
    bill_doc,
    script_two_items,
    tx_doc,
)
from tests.fixtures.pages import cursor_page, cursor_query, paged_page

ITEMS = [ITEM_A, ITEM_B]


class WritePathCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)

    def run_items(self, transport, item_ids=ITEMS, crash_at=None) -> int:
        return self.harness.run(transport, item_ids, crash_at).execute()


class HappyPathTest(WritePathCase):
    def test_two_items_plan_eleven_scopes_walk_in_tiers_and_close_complete(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)

        run_id = self.run_items(transport)

        scopes = self.harness.scopes(run_id)
        planned = {
            key
            for key, row in scopes.items()
            if row["kind"]
            in ("categories", "items", "consents", "identities", "investments", "accounts")
        }
        self.assertEqual(len(planned), 11)
        self.assertEqual(len(scopes), 16)
        self.assertTrue(all(row["outcome"] == "complete" for row in scopes.values()), scopes)
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "complete")

        gets = [u.removeprefix(BASE) for u in transport.urls if not u.endswith("/auth")]
        tier_of = []
        for url in gets:
            if url.startswith("/categories") or url.startswith("/items/"):
                tier_of.append(1)
            elif url.startswith(("/consents", "/identity", "/investments?", "/accounts")):
                tier_of.append(2)
            else:
                tier_of.append(3)
        self.assertEqual(tier_of, sorted(tier_of), gets)
        self.assertEqual(self.harness.run_row(run_id)["api_calls"], len(gets))
        self.assertEqual(
            self.harness.count("SELECT count(*) FROM responses WHERE run_id=?", run_id), len(gets)
        )

    def test_bills_scopes_exist_only_for_credit_accounts(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)

        run_id = self.run_items(transport)

        scopes = self.harness.scopes(run_id)
        self.assertIn(("bills", ACCOUNT_A2), scopes)
        self.assertNotIn(("bills", ACCOUNT_A1), scopes)
        self.assertNotIn(("bills", ACCOUNT_B1), scopes)
        self.assertIn(("investment_transactions", INVESTMENT_A1), scopes)

    def test_dated_kinds_record_their_min_date_and_transactions_their_min_instant(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)

        run_id = self.run_items(transport)

        scopes = self.harness.scopes(run_id)
        self.assertEqual(
            scopes[("investment_transactions", INVESTMENT_A1)]["min_date"], "2026-01-03"
        )
        self.assertEqual(scopes[("bills", ACCOUNT_A2)]["min_date"], "2026-02-10")
        self.assertEqual(scopes[("transactions", ACCOUNT_A2)]["min_date"], "2026-01-10")
        self.assertEqual(
            scopes[("transactions", ACCOUNT_A2)]["min_instant"], "2026-01-10T12:00:00+00:00"
        )
        self.assertIsNone(scopes[("accounts", ITEM_A)]["min_date"])
        self.assertEqual(scopes[("transactions", ACCOUNT_A2)]["rows"], 2)
        self.assertEqual(scopes[("transactions", ACCOUNT_A2)]["infer_absence"], 1)


class ArchiveRowsTest(WritePathCase):
    def test_retries_failures_truncation_and_empty_bodies_are_archived(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        # Reorder the categories queue: 401 then the real page.
        transport.script[f"{BASE}/categories?{PAGE_1}"].appendleft(refused(401))
        # consents of A: 429 then the page.
        transport.script[f"{BASE}/consents?itemId={ITEM_A}&{PAGE_1}"].appendleft(
            refused(429, [["Retry-After", "1"]])
        )
        # identity of B: a transport failure, then the page.
        transport.script[f"{BASE}/identity?itemId={ITEM_B}"].appendleft(
            unreachable("URLError: reset")
        )
        # accounts of B: a truncated body, then the page.
        transport.script[f"{BASE}/accounts?itemId={ITEM_B}&{PAGE_1}"].appendleft(
            truncated(b'{"results": [')
        )
        # investments of B: an empty 404 body (unavailable).
        transport.script[f"{BASE}/investments?itemId={ITEM_B}&{PAGE_1}"].clear()
        transport.answer(f"/investments?itemId={ITEM_B}&{PAGE_1}", raw(404, b""))

        with redirect_stderr(io.StringIO()):
            run_id = self.run_items(transport)

        scopes = self.harness.scopes(run_id)
        by_scope: dict[int, list[tuple]] = {}
        for row in self.harness.responses(run_id):
            by_scope.setdefault(row[1], []).append(row)
        statuses = lambda key: [r[2] for r in by_scope[scopes[key]["id"]]]  # noqa: E731
        self.assertEqual(statuses(("categories", "")), [401, 200])
        self.assertEqual(statuses(("consents", ITEM_A)), [429, 200])
        self.assertEqual(statuses(("identities", ITEM_B)), [0, 200])
        failed = by_scope[scopes[("identities", ITEM_B)]["id"]][0]
        self.assertEqual(failed[4], "URLError: reset")
        self.assertIsNone(failed[5])
        cut = by_scope[scopes[("accounts", ITEM_B)]["id"]][0]
        self.assertEqual((cut[2], cut[3], cut[4]), (200, 1, "truncated"))
        self.assertIsNone(
            self.harness.connection.execute(
                "SELECT 1 FROM page_derivations WHERE response_id = ?", (cut[0],)
            ).fetchone()
        )
        empty = by_scope[scopes[("investments", ITEM_B)]["id"]][0]
        self.assertEqual((empty[2], empty[5], empty[6]), (404, hashlib.sha256(b"").hexdigest(), 0))
        self.assertEqual(scopes[("investments", ITEM_B)]["outcome"], "unavailable")
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "complete")

    def test_archive_failure_stops_the_walk_and_fails_the_run(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)

        def crash_at(name: str, **context) -> None:
            if name == "after_archive_commit" and "/v2/transactions" in context["url"]:
                raise sqlite3.OperationalError("disk I/O error")

        run_id = self.run_items(transport, crash_at=crash_at)

        transactions = [u for u in transport.urls if "/v2/transactions" in u]
        self.assertEqual(len(transactions), 1)
        row = self.harness.run_row(run_id)
        self.assertEqual(row["outcome"], "failed")
        self.assertIn("disk I/O error", row["note"])
        self.assertEqual(
            self.harness.count(
                "SELECT count(*) FROM scopes WHERE run_id=? AND outcome='pending'", run_id
            ),
            0,
        )


class DerivationTest(WritePathCase):
    def test_valid_page_1_survives_a_malformed_page_2(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second = cursor_query(ACCOUNT_A1, after="c1")
        transport.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
            ok(cursor_page([tx_doc("tx-a1-0001", ACCOUNT_A1)], second)),
        )
        transport.answer(
            f"/v2/transactions{second}", raw(200, b'{"results": [{"id": "x", "amount": NaN}]}')
        )

        run_id = self.run_items(transport)

        scope = self.harness.scopes(run_id)[("transactions", ACCOUNT_A1)]
        self.assertEqual(scope["outcome"], "incomplete")
        self.assertEqual(scope["infer_absence"], 0)
        self.assertEqual(scope["rows"], 1)
        derivations = self.harness.connection.execute(
            "SELECT p.valid FROM page_derivations p JOIN responses r ON r.id = p.response_id"
            " WHERE r.scope_id = ? ORDER BY r.id",
            (scope["id"],),
        ).fetchall()
        self.assertEqual([v for (v,) in derivations], [1, 0])
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "partial")

    def test_a_document_without_a_usable_id_invalidates_its_page(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/bills?accountId={ACCOUNT_A2}&{PAGE_1}"].clear()
        transport.answer(
            f"/bills?accountId={ACCOUNT_A2}&{PAGE_1}", ok(paged_page([{"id": "", "dueDate": "x"}]))
        )

        run_id = self.run_items(transport)

        scope = self.harness.scopes(run_id)[("bills", ACCOUNT_A2)]
        self.assertEqual(scope["outcome"], "incomplete")
        self.assertIn("no usable id", scope["error"])
        self.assertEqual(scope["rows"], 0)

    def test_repeated_id_with_same_body_is_one_document_two_sightings_no_absence(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second = cursor_query(ACCOUNT_A1, after="c1")
        same = tx_doc("tx-a1-0001", ACCOUNT_A1)
        transport.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_A1)}", ok(cursor_page([same], second))
        )
        transport.answer(f"/v2/transactions{second}", ok(cursor_page([dict(same)])))

        run_id = self.run_items(transport)

        scope = self.harness.scopes(run_id)[("transactions", ACCOUNT_A1)]
        self.assertEqual(
            (scope["outcome"], scope["infer_absence"], scope["rows"]), ("complete", 0, 1)
        )
        self.assertEqual(
            self.harness.count("SELECT count(*) FROM observations WHERE scope_id=?", scope["id"]), 2
        )
        self.assertEqual(
            self.harness.count("SELECT count(*) FROM ambiguities WHERE run_id=?", run_id), 0
        )

    def test_repeated_id_with_different_bodies_is_ambiguous(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second = cursor_query(ACCOUNT_A1, after="c1")
        transport.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
            ok(cursor_page([tx_doc("tx-a1-0001", ACCOUNT_A1)], second)),
        )
        changed = tx_doc("tx-a1-0001", ACCOUNT_A1)
        changed["amount"] = -99.0
        transport.answer(f"/v2/transactions{second}", ok(cursor_page([changed])))

        run_id = self.run_items(transport)

        row = self.harness.connection.execute(
            "SELECT reason, candidate_parents FROM ambiguities WHERE run_id=?", (run_id,)
        ).fetchone()
        self.assertEqual(row[0], "two bodies in one run")
        self.assertEqual(row[1], f'["{ACCOUNT_A1}"]')
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "partial")

    def test_same_bill_under_two_accounts_is_ambiguous_in_either_walk_order(self) -> None:
        results = []
        for order in ("a-first", "b-first"):
            harness = LedgerHarness()
            self.addCleanup(harness.close)
            transport = harness.transport()
            script_two_items(transport)
            # Make ACCOUNT_B1 a credit card too, with the very same bill as ACCOUNT_A2.
            transport.script[f"{BASE}/accounts?itemId={ITEM_B}&{PAGE_1}"].clear()
            transport.answer(
                f"/accounts?itemId={ITEM_B}&{PAGE_1}",
                ok(paged_page([account_doc(ACCOUNT_B1, ITEM_B, "CREDIT")])),
            )
            transport.answer(
                f"/bills?accountId={ACCOUNT_B1}&{PAGE_1}", ok(paged_page([bill_doc("bill-0001")]))
            )
            items = [ITEM_A, ITEM_B] if order == "a-first" else [ITEM_B, ITEM_A]

            run_id = harness.run(transport, items).execute()

            row = harness.connection.execute(
                "SELECT candidate_parents, reason FROM ambiguities WHERE run_id=?", (run_id,)
            ).fetchone()
            results.append((row, harness.run_row(run_id)["outcome"]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0][0], f'["{ACCOUNT_A2}", "{ACCOUNT_B1}"]')
        self.assertEqual(results[0][0][1], "two parents in one run")
        self.assertEqual(results[0][1], "partial")

    def test_observations_on_both_sides_of_local_midnight_carry_different_days(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        # Local midnight is 03:00Z in January; start two seconds before it so the
        # tier-1 requests land on the 1st and everything later on the 2nd.
        self.harness.clock.t = 24 * 3600 + 3 * 3600 - 2 - 0.15 * 2

        run_id = self.run_items(transport)

        days = {
            day
            for (day,) in self.harness.connection.execute(
                "SELECT DISTINCT observed_day FROM observations WHERE run_id=?", (run_id,)
            )
        }
        self.assertEqual(days, {"2026-01-01", "2026-01-02"})

    def test_deriving_a_page_twice_changes_nothing(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        run_id = self.run_items(transport)
        scope_row = self.harness.scopes(run_id)[("transactions", ACCOUNT_A2)]
        scope = Scope(scope_row["id"], run_id, "transactions", ACCOUNT_A2, 1, "complete")
        response_id = self.harness.connection.execute(
            "SELECT id, received_at, received_day FROM responses WHERE scope_id=?", (scope.id,)
        ).fetchone()
        page = Page(
            response_id[0], [tx_doc("tx-a2-0001", ACCOUNT_A2), tx_doc("tx-a2-0002", ACCOUNT_A2)]
        )
        before = [
            self.harness.count(f"SELECT count(*) FROM {table}")
            for table in ("documents", "versions", "observations", "page_derivations")
        ]

        derived = derive_page(self.harness.connection, scope, page, response_id[1], response_id[2])

        after = [
            self.harness.count(f"SELECT count(*) FROM {table}")
            for table in ("documents", "versions", "observations", "page_derivations")
        ]
        self.assertFalse(derived)
        self.assertEqual(before, after)


class SkippedChildrenTest(WritePathCase):
    def test_a_refused_item_skips_its_planned_scopes_and_children(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.run_items(first)
        second = self.harness.transport()
        script_two_items(second)
        second.script[f"{BASE}/items/{ITEM_A}"].clear()
        second.answer(f"/items/{ITEM_A}", refused(500))

        run_id = self.run_items(second)

        scopes = self.harness.scopes(run_id)
        for kind in ("consents", "identities", "investments", "accounts"):
            self.assertEqual(scopes[(kind, ITEM_A)]["outcome"], "skipped", kind)
            self.assertEqual(scopes[(kind, ITEM_A)]["expected"], 1)
        for key in (
            ("transactions", ACCOUNT_A1),
            ("transactions", ACCOUNT_A2),
            ("bills", ACCOUNT_A2),
            ("investment_transactions", INVESTMENT_A1),
        ):
            self.assertEqual(scopes[key]["outcome"], "skipped", key)
            self.assertEqual(scopes[key]["expected"], 1)
            self.assertEqual(scopes[key]["rows"], 0)
        self.assertEqual(scopes[("transactions", ACCOUNT_B1)]["outcome"], "complete")
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "partial")
        self.assertFalse(any(u.startswith(f"{BASE}/accounts?itemId={ITEM_A}") for u in second.urls))

    def test_an_account_omitted_by_an_absence_safe_walk_gets_unowed_skipped_children(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.run_items(first)
        second = self.harness.transport()
        script_two_items(second)
        second.script[f"{BASE}/accounts?itemId={ITEM_A}&{PAGE_1}"].clear()
        second.answer(
            f"/accounts?itemId={ITEM_A}&{PAGE_1}", ok(paged_page([account_doc(ACCOUNT_A1, ITEM_A)]))
        )

        run_id = self.run_items(second)

        scopes = self.harness.scopes(run_id)
        self.assertEqual(scopes[("transactions", ACCOUNT_A1)]["outcome"], "complete")
        self.assertEqual(
            (
                scopes[("transactions", ACCOUNT_A2)]["outcome"],
                scopes[("transactions", ACCOUNT_A2)]["expected"],
            ),
            ("skipped", 0),
        )
        self.assertEqual(
            (scopes[("bills", ACCOUNT_A2)]["outcome"], scopes[("bills", ACCOUNT_A2)]["expected"]),
            ("skipped", 0),
        )
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "complete")

    def test_an_incomplete_accounts_walk_walks_what_it_saw_and_skips_the_rest_as_owed(self) -> None:
        first = self.harness.transport()
        script_two_items(first)
        self.run_items(first)
        second = self.harness.transport()
        script_two_items(second)
        second.script[f"{BASE}/accounts?itemId={ITEM_A}&{PAGE_1}"].clear()
        second.answer(
            f"/accounts?itemId={ITEM_A}&{PAGE_1}",
            ok(paged_page([account_doc(ACCOUNT_A1, ITEM_A)], page=1, total_pages=2)),
        )
        second.answer(f"/accounts?itemId={ITEM_A}&pageSize=500&page=2", refused(500))

        run_id = self.run_items(second)

        scopes = self.harness.scopes(run_id)
        self.assertEqual(scopes[("accounts", ITEM_A)]["outcome"], "incomplete")
        self.assertEqual(scopes[("transactions", ACCOUNT_A1)]["outcome"], "complete")
        self.assertEqual(
            (
                scopes[("transactions", ACCOUNT_A2)]["outcome"],
                scopes[("transactions", ACCOUNT_A2)]["expected"],
            ),
            ("skipped", 1),
        )
        self.assertEqual(
            (scopes[("bills", ACCOUNT_A2)]["outcome"], scopes[("bills", ACCOUNT_A2)]["expected"]),
            ("skipped", 1),
        )
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "partial")

    def test_an_ambiguous_parent_gets_owed_skipped_children_and_is_not_walked(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        # The same account id under both items: ambiguous across the run.
        transport.script[f"{BASE}/accounts?itemId={ITEM_B}&{PAGE_1}"].clear()
        transport.answer(
            f"/accounts?itemId={ITEM_B}&{PAGE_1}", ok(paged_page([account_doc(ACCOUNT_A1, ITEM_B)]))
        )

        run_id = self.run_items(transport)

        scopes = self.harness.scopes(run_id)
        self.assertEqual(
            (
                scopes[("transactions", ACCOUNT_A1)]["outcome"],
                scopes[("transactions", ACCOUNT_A1)]["expected"],
            ),
            ("skipped", 1),
        )
        self.assertFalse(any(cursor_query(ACCOUNT_A1) in u for u in transport.urls))
        self.assertEqual(
            self.harness.count("SELECT count(*) FROM ambiguities WHERE run_id=?", run_id), 1
        )
        self.assertEqual(self.harness.run_row(run_id)["outcome"], "partial")


class SelfCheckTest(WritePathCase):
    def test_a_clean_run_passes_and_a_crossed_link_is_reported(self) -> None:
        transport = self.harness.transport()
        script_two_items(transport)
        run_id = self.run_items(transport)
        db = self.harness.connection
        self.assertEqual(self_check(db, run_id), [])

        # An observation filed under the wrong scope, inserted behind the write path's back.
        scopes = self.harness.scopes(run_id)
        wrong_scope = scopes[("transactions", ACCOUNT_B1)]["id"]
        right_scope = scopes[("transactions", ACCOUNT_A1)]["id"]
        response_id, document_id, sha = db.execute(
            "SELECT response_id, document_id, sha256 FROM observations WHERE scope_id=?",
            (right_scope,),
        ).fetchone()
        db.execute(
            "INSERT INTO observations (run_id, scope_id, response_id, position, document_id,"
            " parent, sha256, observed_at, observed_day) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                wrong_scope,
                response_id,
                7,
                document_id,
                ACCOUNT_B1,
                sha,
                "2026-01-01T12:00:00+00:00",
                "2026-01-01",
            ),
        )

        problems = self_check(db, run_id)

        self.assertTrue(any("disagreeing with their response" in p for p in problems), problems)
        self.assertTrue(any("rows differ" in p for p in problems), problems)


class ItemIdsTest(unittest.TestCase):
    def test_duplicates_are_dropped_in_first_seen_order_and_logged(self) -> None:
        lines: list[str] = []
        self.assertEqual(parse_item_ids("a, a ,b", lines.append), ["a", "b"])
        self.assertEqual(len(lines), 1)

    def test_an_empty_id_is_a_configuration_error(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_item_ids("a,,b", lambda line: None)
        with self.assertRaises(ConfigurationError):
            parse_item_ids("", lambda line: None)


if __name__ == "__main__":
    unittest.main()
