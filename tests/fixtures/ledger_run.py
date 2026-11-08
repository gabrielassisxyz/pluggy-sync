"""A whole synthetic account tree scripted onto the fake transport, and the
helpers a ledger-run test needs to read the result back."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from typing import Any

from pluggysync.ledger import open_ledger
from pluggysync.run import LedgerRun
from pluggysync.walk import Client
from tests.fakes import FakeClock, FakeTransport, ok
from tests.fixtures.pages import cursor_page, cursor_query, paged_page

ITEM_A = "item-000a"
ITEM_B = "item-000b"
ACCOUNT_A1 = "account-00a1"  # BANK
ACCOUNT_A2 = "account-00a2"  # CREDIT
ACCOUNT_B1 = "account-00b1"  # BANK
INVESTMENT_A1 = "investment-00a1"

PAGE_1 = "pageSize=500&page=1"


def item_doc(item_id: str, updated_at: str = "2026-01-01T06:00:00+00:00") -> dict[str, Any]:
    return {
        "id": item_id,
        "connector": {"id": 1, "name": "Synthetic Bank"},
        "status": "UPDATED",
        "executionStatus": "SUCCESS",
        "lastUpdatedAt": updated_at,
    }


def account_doc(account_id: str, item_id: str, kind: str = "BANK") -> dict[str, Any]:
    return {
        "id": account_id,
        "itemId": item_id,
        "type": kind,
        "subtype": "CREDIT_CARD" if kind == "CREDIT" else "CHECKING_ACCOUNT",
        "name": f"account {account_id[-2:]}",
        "balance": 100.0,
        "currencyCode": "BRL",
    }


def investment_doc(investment_id: str, item_id: str) -> dict[str, Any]:
    return {"id": investment_id, "itemId": item_id, "type": "FIXED_INCOME", "balance": 500.0}


def tx_doc(tx_id: str, account_id: str, date: str = "2026-01-10T12:00:00.000Z") -> dict[str, Any]:
    return {"id": tx_id, "accountId": account_id, "date": date, "amount": -10.0, "status": "POSTED"}


def investment_tx_doc(tx_id: str, date: str = "2026-01-03T00:00:00.000Z") -> dict[str, Any]:
    return {"id": tx_id, "date": date, "amount": 5.0, "type": "BUY"}


def bill_doc(bill_id: str, due: str = "2026-02-10T00:00:00.000Z") -> dict[str, Any]:
    return {"id": bill_id, "dueDate": due, "totalAmount": 300.0}


def consent_doc(item_id: str, expires_at: str = "2027-01-01T00:00:00Z") -> dict[str, Any]:
    return {"id": f"consent-{item_id[-1]}", "itemId": item_id, "expiresAt": expires_at}


def identity_doc(item_id: str) -> dict[str, Any]:
    return {"id": f"identity-{item_id[-1]}", "itemId": item_id, "fullName": "Placeholder"}


def script_item(
    transport: FakeTransport, item_id: str, accounts: list[dict], investments: list[dict]
) -> None:
    # Fresh relative to the fake clock: synced six hours ago, consent good for a year.
    now = datetime.fromisoformat(transport.clock.now())
    updated = (now - timedelta(hours=6)).isoformat()
    expires = (now + timedelta(days=365)).isoformat()
    transport.answer(f"/items/{item_id}", ok(item_doc(item_id, updated)))
    transport.answer(
        f"/consents?itemId={item_id}&{PAGE_1}", ok(paged_page([consent_doc(item_id, expires)]))
    )
    transport.answer(f"/identity?itemId={item_id}", ok(identity_doc(item_id)))
    transport.answer(f"/investments?itemId={item_id}&{PAGE_1}", ok(paged_page(investments)))
    transport.answer(f"/accounts?itemId={item_id}&{PAGE_1}", ok(paged_page(accounts)))


def script_children(transport: FakeTransport) -> None:
    transport.answer(
        f"/investments/{INVESTMENT_A1}/transactions?{PAGE_1}",
        ok(paged_page([investment_tx_doc("itx-0001")])),
    )
    transport.answer(
        f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
        ok(cursor_page([tx_doc("tx-a1-0001", ACCOUNT_A1)])),
    )
    transport.answer(
        f"/v2/transactions{cursor_query(ACCOUNT_A2)}",
        ok(cursor_page([tx_doc("tx-a2-0001", ACCOUNT_A2), tx_doc("tx-a2-0002", ACCOUNT_A2)])),
    )
    transport.answer(
        f"/bills?accountId={ACCOUNT_A2}&{PAGE_1}", ok(paged_page([bill_doc("bill-0001")]))
    )
    transport.answer(
        f"/v2/transactions{cursor_query(ACCOUNT_B1)}",
        ok(cursor_page([tx_doc("tx-b1-0001", ACCOUNT_B1)])),
    )


def script_two_items(transport: FakeTransport) -> None:
    """Two items: A with a BANK and a CREDIT account and one investment, B with one BANK account."""
    transport.answer(
        f"/categories?{PAGE_1}",
        ok(paged_page([{"id": "01000000", "description": "Income", "parentId": None}])),
    )
    script_item(
        transport,
        ITEM_A,
        [account_doc(ACCOUNT_A1, ITEM_A), account_doc(ACCOUNT_A2, ITEM_A, "CREDIT")],
        [investment_doc(INVESTMENT_A1, ITEM_A)],
    )
    script_item(transport, ITEM_B, [account_doc(ACCOUNT_B1, ITEM_B)], [])
    script_children(transport)


class LedgerHarness:
    """A ledger file, a clock, and a fresh fake transport per run."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "ledger.db")
        self.clock = FakeClock()
        self.clock.t = 12 * 3600  # 2026-01-01T12:00Z: safely inside one local day
        self.connection = open_ledger(self.path)

    def close(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def reopen(self) -> None:
        """What a process restart does: a new connection to the same file."""
        self.connection.close()
        self.connection = open_ledger(self.path)

    def transport(self) -> FakeTransport:
        return FakeTransport(self.clock)

    def run(self, transport: FakeTransport, item_ids: list[str], crash_at: Any = None) -> LedgerRun:
        def make_client(on_attempt: Any) -> Client:
            return Client("client-id", "client-secret", on_attempt, transport, self.clock)

        return LedgerRun(self.connection, item_ids, make_client, self.clock, "test", crash_at)

    # -- readers --------------------------------------------------------------

    def scopes(self, run_id: int) -> dict[tuple[str, str], dict[str, Any]]:
        self.connection.row_factory = sqlite3.Row
        rows = self.connection.execute(
            "SELECT * FROM scopes WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        self.connection.row_factory = None
        return {(row["kind"], row["parent"]): dict(row) for row in rows}

    def run_row(self, run_id: int) -> dict[str, Any]:
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.connection.row_factory = None
        return dict(row)

    def count(self, sql: str, *parameters: Any) -> int:
        return int(self.connection.execute(sql, parameters).fetchone()[0])

    def responses(self, run_id: int) -> list[tuple[Any, ...]]:
        return self.connection.execute(
            "SELECT id, scope_id, status, truncated, error, body_sha256, raw_bytes FROM responses"
            " WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
