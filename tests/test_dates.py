"""Business dates in the domain the plan fixes per kind."""

from __future__ import annotations

import unittest

from pluggysync.dates import business_date, parse_instant, transaction_instant


class BusinessDateTest(unittest.TestCase):
    def test_transaction_date_is_the_utc_date_of_the_instant(self) -> None:
        # 23:30 in Sao Paulo on the 9th is already the 10th in UTC; UTC wins.
        self.assertEqual(
            business_date("transactions", {"date": "2026-01-10T02:30:00.000Z"}), "2026-01-10"
        )
        self.assertEqual(
            business_date("transactions", {"date": "2026-01-09T23:30:00-03:00"}), "2026-01-10"
        )

    def test_midnight_utc_business_dates_keep_their_date_part(self) -> None:
        self.assertEqual(
            business_date("investment_transactions", {"date": "2026-01-03T00:00:00.000Z"}),
            "2026-01-03",
        )
        self.assertEqual(
            business_date("bills", {"dueDate": "2026-02-10T00:00:00.000Z"}), "2026-02-10"
        )

    def test_undated_kinds_have_no_business_date(self) -> None:
        self.assertIsNone(business_date("accounts", {"date": "2026-01-10T00:00:00Z"}))

    def test_unparsable_or_naive_or_missing_dates_are_none(self) -> None:
        self.assertIsNone(business_date("transactions", {"date": "yesterday"}))
        self.assertIsNone(business_date("transactions", {"date": "2026-01-10T00:00:00"}))
        self.assertIsNone(business_date("transactions", {}))
        self.assertIsNone(business_date("bills", {"dueDate": 20260210}))
        self.assertIsNone(parse_instant(None))

    def test_transaction_instant_is_normalised_to_utc(self) -> None:
        self.assertEqual(
            transaction_instant({"date": "2026-01-09T23:30:00-03:00"}), "2026-01-10T02:30:00+00:00"
        )
        self.assertIsNone(transaction_instant({"date": "nope"}))


if __name__ == "__main__":
    unittest.main()
