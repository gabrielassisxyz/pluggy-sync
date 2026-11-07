"""The walk contract: every attempt archived before parsing, every walk saying
whether it finished, and the refusal matrix that separates "not available
here" from a cut walk."""

from __future__ import annotations

import unittest
from itertools import pairwise

from pluggysync.walk import BASE, REQUEST_GAP_SECONDS, Client, FetchResult
from tests.fakes import (
    FakeClock,
    FakeTransport,
    RaisingArchive,
    RecordingArchive,
    ok,
    raw,
    refused,
    truncated,
    unreachable,
)
from tests.fixtures.pages import (
    ITEM_ID,
    OTHER_ACCOUNT_ID,
    account,
    category,
    cursor_page,
    cursor_query,
    identity,
    item,
    paged_page,
    transaction,
)

TRANSACTIONS = "/v2/transactions"
PAGE_1 = "pageSize=500&page=1"
PAGE_2 = "pageSize=500&page=2"


class WalkCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.transport = FakeTransport(self.clock)
        self.archive = RecordingArchive(self.clock)
        self.client = Client("client-id", "client-secret", self.archive, self.transport, self.clock)

    def assertIncomplete(self, result: FetchResult, fragment: str) -> None:
        self.assertEqual(result.outcome, "incomplete", result)
        self.assertIsNotNone(result.error)
        self.assertIn(fragment, result.error or "")


class CursorWalkTest(WalkCase):
    def test_500_on_page_2_keeps_page_1_and_archives_both_attempts(self) -> None:
        first = cursor_query()
        second = cursor_query(after="cursor-a")
        self.transport.answer(
            f"{TRANSACTIONS}{first}", ok(cursor_page([transaction(1), transaction(2)], second))
        )
        self.transport.answer(f"{TRANSACTIONS}{second}", refused(500))

        result = self.client.cursored(TRANSACTIONS, first)

        self.assertIncomplete(result, "HTTP 500 at page 2")
        self.assertEqual(len(result.pages), 1)
        self.assertEqual([d["id"] for d in result.documents], ["tx-0001", "tx-0002"])
        self.assertEqual(result.pages[0].attempt_id, 1)
        self.assertEqual([a.status for a in self.archive.attempts], [200, 500])
        self.assertEqual(result.status, 500)

    def test_relative_next_is_appended_to_the_endpoint_until_absent(self) -> None:
        q1, q2, q3 = cursor_query(), cursor_query(after="a"), cursor_query(after="b")
        self.transport.answer(f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], q2)))
        self.transport.answer(f"{TRANSACTIONS}{q2}", ok(cursor_page([transaction(2)], q3)))
        self.transport.answer(f"{TRANSACTIONS}{q3}", ok(cursor_page([transaction(3)])))

        result = self.client.cursored(TRANSACTIONS, q1)

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(len(result.documents), 3)
        self.assertEqual([p.next for p in result.pages], [q2, q3, None])
        gets = [u for u in self.transport.urls if not u.endswith("/auth")]
        self.assertTrue(all(TRANSACTIONS in u for u in gets), gets)

    def test_next_as_a_path_naming_the_same_endpoint_is_accepted(self) -> None:
        q1, q2 = cursor_query(), cursor_query(after="a")
        self.transport.answer(
            f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], f"{TRANSACTIONS}{q2}"))
        )
        self.transport.answer(f"{TRANSACTIONS}{q2}", ok(cursor_page([transaction(2)])))

        self.assertEqual(self.client.cursored(TRANSACTIONS, q1).outcome, "complete")

    def test_next_naming_another_account_is_incomplete(self) -> None:
        q1 = cursor_query()
        elsewhere = cursor_query(OTHER_ACCOUNT_ID, after="a")
        self.transport.answer(f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], elsewhere)))

        result = self.client.cursored(TRANSACTIONS, q1)

        self.assertIncomplete(result, "another account")
        self.assertEqual(len(result.documents), 1)
        self.assertEqual(len(self.archive.attempts), 1)

    def test_next_that_does_not_advance_is_incomplete(self) -> None:
        q1 = cursor_query()
        self.transport.answer(f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], q1)))

        self.assertIncomplete(self.client.cursored(TRANSACTIONS, q1), "does not advance")

    def test_next_as_an_absolute_url_is_incomplete(self) -> None:
        q1 = cursor_query()
        self.transport.answer(
            f"{TRANSACTIONS}{q1}",
            ok(cursor_page([transaction(1)], f"https://elsewhere.example{TRANSACTIONS}{q1}")),
        )

        self.assertIncomplete(self.client.cursored(TRANSACTIONS, q1), "does not name")


class RetryTest(WalkCase):
    def test_429_then_200_archives_both_in_order_and_the_first_before_the_sleep(self) -> None:
        path = f"/accounts?itemId={ITEM_ID}&{PAGE_1}"
        self.transport.answer(
            path, refused(429, [["Retry-After", "abc"]]), ok(paged_page([account(1)]))
        )

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual([a.status for a in self.archive.attempts], [429, 200])
        # Before the 429 was archived the clock had slept only the request gaps.
        self.assertTrue(all(s == REQUEST_GAP_SECONDS for s in self.archive.sleeps_seen[0]))
        self.assertIn(60, self.clock.sleeps)

    def test_retry_after_is_honoured_and_capped(self) -> None:
        path = f"/accounts?itemId={ITEM_ID}&{PAGE_1}"
        self.transport.answer(
            path,
            refused(429, [["retry-after", "7"]]),
            refused(429, [["retry-after", "99999"]]),
            ok(paged_page([account(1)])),
        )

        self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertIn(7, self.clock.sleeps)
        self.assertIn(300, self.clock.sleeps)
        self.assertNotIn(99999, self.clock.sleeps)

    def test_401_renews_the_key_and_retries_the_same_page(self) -> None:
        path = f"/accounts?itemId={ITEM_ID}&{PAGE_1}"
        self.transport.answer(path, refused(401), ok(paged_page([account(1)])))

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(self.transport.api_keys_sent, ["key-1", "key-2"])
        self.assertEqual([a.status for a in self.archive.attempts], [401, 200])

    def test_truncated_body_is_archived_never_parsed_and_retried(self) -> None:
        path = f"/accounts?itemId={ITEM_ID}&{PAGE_1}"
        self.transport.answer(path, truncated(b'{"results": [{"id": "acc'), ok(paged_page([])))

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(self.archive.attempts[0].error, "truncated")
        self.assertEqual(self.archive.attempts[0].body_bytes, b'{"results": [{"id": "acc')

    def test_transport_failures_are_bounded_and_end_incomplete(self) -> None:
        path = f"/accounts?itemId={ITEM_ID}&{PAGE_1}"
        self.transport.answer(path, unreachable(), unreachable(), unreachable())

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertIncomplete(result, "transport")
        self.assertEqual(result.status, 0)
        self.assertEqual(len(self.archive.attempts), 3)
        self.assertEqual([a.status for a in self.archive.attempts], [0, 0, 0])

    def test_raising_callback_stops_the_walk_before_a_second_request(self) -> None:
        archive = RaisingArchive()
        client = Client("id", "secret", archive, self.transport, self.clock)
        q1, q2 = cursor_query(), cursor_query(after="a")
        self.transport.answer(f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], q2)))
        self.transport.answer(f"{TRANSACTIONS}{q2}", ok(cursor_page([transaction(2)])))

        result = client.cursored(TRANSACTIONS, q1)

        self.assertIncomplete(result, "on_attempt raised")
        self.assertEqual(archive.calls, 1)
        self.assertEqual(len([u for u in self.transport.urls if TRANSACTIONS in u]), 1)
        self.assertEqual(result.pages, [])


class RefusalMatrixTest(WalkCase):
    def test_404_on_identity_as_the_first_attempt_is_unavailable(self) -> None:
        self.transport.answer(f"/identity?itemId={ITEM_ID}", refused(404))

        result = self.client.single(f"/identity?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "unavailable")
        self.assertIsNone(result.error)
        self.assertEqual(result.status, 404)
        self.assertEqual(result.pages, [])

    def test_404_on_identity_after_a_401_retry_is_still_unavailable(self) -> None:
        self.transport.answer(f"/identity?itemId={ITEM_ID}", refused(401), refused(404))

        result = self.client.single(f"/identity?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "unavailable")
        self.assertEqual([a.status for a in self.archive.attempts], [401, 404])

    def test_403_on_investments_first_page_is_unavailable(self) -> None:
        self.transport.answer(f"/investments?itemId={ITEM_ID}&{PAGE_1}", refused(403))

        self.assertEqual(self.client.paged(f"/investments?itemId={ITEM_ID}").outcome, "unavailable")

    def test_404_on_an_item_is_incomplete(self) -> None:
        self.transport.answer(f"/items/{ITEM_ID}", refused(404))

        result = self.client.single(f"/items/{ITEM_ID}", expected_id=ITEM_ID)

        self.assertIncomplete(result, "HTTP 404 at page 1")
        self.assertEqual(result.status, 404)

    def test_403_on_page_2_of_accounts_is_incomplete(self) -> None:
        self.transport.answer(
            f"/accounts?itemId={ITEM_ID}&{PAGE_1}", ok(paged_page([account(1)], 1, 2))
        )
        self.transport.answer(f"/accounts?itemId={ITEM_ID}&{PAGE_2}", refused(403))

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertIncomplete(result, "HTTP 403 at page 2")
        self.assertEqual(len(result.documents), 1)

    def test_500_on_identity_is_incomplete_not_unavailable(self) -> None:
        self.transport.answer(f"/identity?itemId={ITEM_ID}", refused(500))

        self.assertIncomplete(self.client.single(f"/identity?itemId={ITEM_ID}"), "HTTP 500")


class BodyShapeTest(WalkCase):
    def paged_with(self, body: bytes) -> FetchResult:
        self.transport.answer(f"/accounts?itemId={ITEM_ID}&{PAGE_1}", raw(200, body))
        return self.client.paged(f"/accounts?itemId={ITEM_ID}")

    def test_duplicate_key_is_incomplete(self) -> None:
        result = self.paged_with(b'{"results": [], "totalPages": 1, "totalPages": 1}')
        self.assertIncomplete(result, "duplicate key")
        self.assertEqual(result.pages[0].error, "invalid JSON: duplicate key 'totalPages'")

    def test_nan_is_incomplete(self) -> None:
        body = b'{"results": [{"id": "a", "balance": NaN}], "totalPages": 1}'
        self.assertIncomplete(self.paged_with(body), "non-finite")

    def test_missing_results_is_incomplete(self) -> None:
        self.assertIncomplete(self.paged_with(b'{"totalPages": 1}'), "no results list")

    def test_boolean_total_pages_is_incomplete(self) -> None:
        self.assertIncomplete(self.paged_with(b'{"results": [], "totalPages": true}'), "count")

    def test_changing_total_pages_is_incomplete(self) -> None:
        self.transport.answer(
            f"/accounts?itemId={ITEM_ID}&{PAGE_1}", ok(paged_page([account(1)], 1, 3))
        )
        self.transport.answer(
            f"/accounts?itemId={ITEM_ID}&{PAGE_2}", ok(paged_page([account(2)], 2, 2))
        )

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertIncomplete(result, "totalPages changed from 3 to 2")
        self.assertEqual(len(result.pages), 2)

    def test_page_answering_for_another_page_is_incomplete(self) -> None:
        body = paged_page([account(1)], page=2, total_pages=1)
        self.transport.answer(f"/accounts?itemId={ITEM_ID}&{PAGE_1}", ok(body))

        self.assertIncomplete(self.client.paged(f"/accounts?itemId={ITEM_ID}"), "page 2 answered")

    def test_item_with_another_id_is_incomplete_and_not_kept(self) -> None:
        self.transport.answer(f"/items/{ITEM_ID}", ok(item("item-9999")))

        result = self.client.single(f"/items/{ITEM_ID}", expected_id=ITEM_ID)

        self.assertIncomplete(result, "not the requested")
        self.assertEqual(result.documents, [])

    def test_identity_without_an_id_is_incomplete(self) -> None:
        self.transport.answer(f"/identity?itemId={ITEM_ID}", ok({"itemId": ITEM_ID}))

        self.assertIncomplete(self.client.single(f"/identity?itemId={ITEM_ID}"), "no id")

    def test_identity_with_an_id_is_complete(self) -> None:
        self.transport.answer(f"/identity?itemId={ITEM_ID}", ok(identity()))

        result = self.client.single(f"/identity?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.documents[0]["id"], "identity-0001")

    def test_empty_results_with_zero_total_pages_is_complete(self) -> None:
        result = self.paged_with(b'{"results": [], "totalPages": 0}')
        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.documents, [])

    def test_categories_page_without_total_pages_and_fewer_than_page_size_is_complete(
        self,
    ) -> None:
        self.transport.answer(
            f"/categories?{PAGE_1}", ok(paged_page([category(n) for n in range(130)], None, None))
        )

        result = self.client.paged("/categories")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(len(result.documents), 130)
        self.assertEqual(len(self.transport.urls), 2)  # auth, then the one page

    def test_full_page_without_total_pages_is_incomplete(self) -> None:
        rows = [category(n) for n in range(500)]
        self.transport.answer(f"/categories?{PAGE_1}", ok(paged_page(rows, None, None)))

        self.assertIncomplete(self.client.paged("/categories"), "no totalPages")

    def test_paged_walk_follows_total_pages(self) -> None:
        self.transport.answer(
            f"/accounts?itemId={ITEM_ID}&{PAGE_1}", ok(paged_page([account(1)], 1, 2))
        )
        self.transport.answer(
            f"/accounts?itemId={ITEM_ID}&{PAGE_2}", ok(paged_page([account(2)], 2, 2))
        )

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertEqual(result.outcome, "complete")
        self.assertEqual([d["id"] for d in result.documents], ["account-0001", "account-0002"])
        self.assertEqual([p.total_pages for p in result.pages], [2, 2])


class AuthTest(WalkCase):
    def test_auth_transport_failure_is_incomplete_and_does_not_raise(self) -> None:
        self.transport.answer("/auth", unreachable("URLError: name resolution failed"))

        result = self.client.paged(f"/accounts?itemId={ITEM_ID}")

        self.assertIncomplete(result, "auth: transport")
        self.assertEqual(result.pages, [])
        self.assertEqual(self.archive.attempts, [])

    def test_auth_refusal_is_incomplete(self) -> None:
        self.transport.answer("/auth", refused(401))

        self.assertIncomplete(self.client.single(f"/items/{ITEM_ID}"), "auth: HTTP 401")

    def test_auth_body_without_a_key_is_incomplete(self) -> None:
        self.transport.answer("/auth", ok({"token": "nope"}))

        self.assertIncomplete(self.client.single(f"/items/{ITEM_ID}"), "no apiKey")

    def test_auth_is_never_archived(self) -> None:
        self.transport.answer(f"/items/{ITEM_ID}", ok(item()))

        self.client.single(f"/items/{ITEM_ID}", expected_id=ITEM_ID)

        self.assertEqual([a.url.endswith("/auth") for a in self.archive.attempts], [False])
        self.assertEqual(self.archive.attempts[0].method, "GET")


class PacingTest(WalkCase):
    def test_requests_are_sequential_with_the_gap_between_them(self) -> None:
        q1, q2 = cursor_query(), cursor_query(after="a")
        self.transport.answer(f"{TRANSACTIONS}{q1}", ok(cursor_page([transaction(1)], q2)))
        self.transport.answer(f"{TRANSACTIONS}{q2}", ok(cursor_page([transaction(2)])))

        self.client.cursored(TRANSACTIONS, q1)

        times = [r.at for r in self.transport.requests]
        self.assertEqual(
            self.transport.urls[1:], [f"{BASE}{TRANSACTIONS}{q1}", f"{BASE}{TRANSACTIONS}{q2}"]
        )
        gaps = [later - earlier for earlier, later in pairwise(times)]
        self.assertTrue(all(gap >= REQUEST_GAP_SECONDS - 1e-9 for gap in gaps), gaps)

    def test_received_at_comes_from_the_injected_clock(self) -> None:
        self.transport.answer(f"/items/{ITEM_ID}", ok(item()))

        self.client.single(f"/items/{ITEM_ID}")

        self.assertTrue(self.archive.attempts[0].received_at.startswith("2026-01-01T00:00:00"))


if __name__ == "__main__":
    unittest.main()
