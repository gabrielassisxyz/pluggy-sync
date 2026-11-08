"""Freshness thresholds on both sides, and the run-level judgement over sightings."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pluggysync.freshness import (
    CONSENT_EXPIRES_WITHIN_DAYS,
    STALE_AFTER_HOURS,
    consent_staleness,
    item_staleness,
    stale_in_run,
)
from tests.fixtures.hand_built import HandBuilt

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


def item(hours_ago: float, status: str = "UPDATED", execution: str = "SUCCESS") -> dict:
    return {
        "id": "item-0001",
        "status": status,
        "executionStatus": execution,
        "lastUpdatedAt": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
    }


def consent(days_ahead: float, revoked: str | None = None) -> dict:
    body = {
        "id": "consent-0001",
        "expiresAt": (NOW + timedelta(days=days_ahead)).isoformat().replace("+00:00", "Z"),
    }
    if revoked is not None:
        body["revokedAt"] = revoked
    return body


class ItemStalenessTest(unittest.TestCase):
    def test_47_hours_is_fresh_and_49_is_stale(self) -> None:
        self.assertIsNone(item_staleness(item(47), NOW))
        self.assertIn("49h old", item_staleness(item(49), NOW) or "")
        self.assertEqual(STALE_AFTER_HOURS, 48)

    def test_a_status_other_than_updated_is_stale(self) -> None:
        self.assertEqual(item_staleness(item(1, status="LOGIN_ERROR"), NOW), "status 'LOGIN_ERROR'")

    def test_an_execution_status_other_than_success_is_stale(self) -> None:
        self.assertEqual(item_staleness(item(1, execution="ERROR"), NOW), "executionStatus 'ERROR'")

    def test_an_unreadable_timestamp_is_stale(self) -> None:
        body = item(1)
        body["lastUpdatedAt"] = "yesterday"
        self.assertIn("unreadable", item_staleness(body, NOW) or "")


class ConsentStalenessTest(unittest.TestCase):
    def test_31_days_is_fresh_and_29_is_stale(self) -> None:
        self.assertIsNone(consent_staleness(consent(31), NOW))
        self.assertIn("in 29 days", consent_staleness(consent(29), NOW) or "")
        self.assertEqual(CONSENT_EXPIRES_WITHIN_DAYS, 30)

    def test_a_consent_without_an_expiry_is_fresh_and_an_unreadable_one_is_stale(self) -> None:
        body = consent(300)
        body["expiresAt"] = None
        self.assertIsNone(consent_staleness(body, NOW))
        body["expiresAt"] = "someday"
        self.assertIn("unreadable", consent_staleness(body, NOW) or "")

    def test_a_revoked_consent_is_stale(self) -> None:
        self.assertIn(
            "revokedAt", consent_staleness(consent(300, revoked="2026-01-01T00:00:00Z"), NOW) or ""
        )


class StaleInRunTest(unittest.TestCase):
    def test_the_last_sighting_in_the_run_is_judged(self) -> None:
        ledger = HandBuilt()
        self.addCleanup(ledger.close)
        run = ledger.run()
        items = ledger.scope(run, "items", "item-0001")
        ledger.sight(run, items, item(72), parent="item-0001")
        ledger.sight(run, items, item(2), parent="item-0001")
        consents = ledger.scope(run, "consents", "item-0001")
        ledger.sight(run, consents, consent(10))

        stale = stale_in_run(ledger.db, run, NOW)

        self.assertEqual([(s.kind, s.provider_id) for s in stale], [("consent", "consent-0001")])
        self.assertIn("in 10 days", stale[0].line())


if __name__ == "__main__":
    unittest.main()
