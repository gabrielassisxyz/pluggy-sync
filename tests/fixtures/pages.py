"""Builders for the three response shapes the walk understands."""

from __future__ import annotations

from typing import Any

ITEM_ID = "item-0001"
ACCOUNT_ID = "account-0001"
OTHER_ACCOUNT_ID = "account-0002"


def transaction(n: int, account_id: str = ACCOUNT_ID) -> dict[str, Any]:
    return {
        "id": f"tx-{n:04d}",
        "accountId": account_id,
        "date": f"2026-01-{(n % 28) + 1:02d}T00:00:00.000Z",
        "description": f"purchase {n}",
        "amount": -10.0 * n,
        "currencyCode": "BRL",
        "type": "DEBIT",
        "status": "POSTED",
    }


def account(n: int = 1, item_id: str = ITEM_ID) -> dict[str, Any]:
    return {
        "id": f"account-{n:04d}",
        "itemId": item_id,
        "type": "BANK",
        "subtype": "CHECKING_ACCOUNT",
        "name": f"account {n}",
        "balance": 100.0 * n,
        "currencyCode": "BRL",
    }


def category(n: int) -> dict[str, Any]:
    return {"id": f"{n:08d}", "description": f"category {n}", "parentId": None}


def item(item_id: str = ITEM_ID) -> dict[str, Any]:
    return {
        "id": item_id,
        "connector": {"id": 1, "name": "Synthetic Bank"},
        "status": "UPDATED",
        "executionStatus": "SUCCESS",
    }


def identity(item_id: str = ITEM_ID) -> dict[str, Any]:
    return {"id": "identity-0001", "itemId": item_id, "fullName": "Placeholder Person"}


def paged_page(
    results: list[dict[str, Any]], page: int | None = None, total_pages: Any = 1
) -> dict[str, Any]:
    """`total_pages=None` leaves the key out, the way `/categories` may answer."""
    body: dict[str, Any] = {"results": results}
    if page is not None:
        body["page"] = page
    if total_pages is not None:
        body["totalPages"] = total_pages
    return body


def cursor_page(results: list[dict[str, Any]], nxt: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"results": results}
    if nxt is not None:
        body["next"] = nxt
    return body


def cursor_query(account_id: str = ACCOUNT_ID, after: str | None = None) -> str:
    return f"?accountId={account_id}" + (f"&after={after}" if after else "")
