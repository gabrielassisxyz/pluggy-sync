"""Business dates per kind, in the domain the plan fixes for each.

`transactions.date` is an instant and its UTC date is used, never the local
day: the provider's semantics are unverified, and a local shift would move
rows across midnight by up to three hours. `investment_transactions.date` and
`bills.dueDate` are midnight-UTC business dates whose date part is taken as
written. A value that does not parse yields None, and the caller treats the
page as undated: no coverage is claimed over a date it cannot read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

DATE_FIELDS = {
    "transactions": "date",
    "investment_transactions": "date",
    "bills": "dueDate",
}


def parse_instant(value: Any) -> datetime | None:
    """An aware datetime, or None. A naive string has no instant to offer."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def business_date(kind: str, document: dict[str, Any]) -> str | None:
    field = DATE_FIELDS.get(kind)
    if field is None:
        return None
    value = document.get(field)
    moment = parse_instant(value)
    if moment is None:
        return None
    if kind == "transactions":
        return moment.astimezone(UTC).date().isoformat()
    return str(value)[:10]


def transaction_instant(document: dict[str, Any]) -> str | None:
    """The transaction's instant normalised to UTC, for `min_instant`."""
    moment = parse_instant(document.get("date"))
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat()
