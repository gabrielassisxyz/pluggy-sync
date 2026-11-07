"""Walks: the pages behind one endpoint, and whether the walk finished.

Three shapes cover the whole API. `paged` is the page/totalPages style
(accounts, bills, investments, categories), `cursored` the `next` style of
`/v2/transactions`, `single` an endpoint answering one document. Each returns a
`FetchResult` whose `outcome` is `complete`, `incomplete` or `unavailable`: a
walk that stopped short is never mistaken for a short one, which the previous
generator-based walk could not promise.

Every HTTP attempt, retries included, reaches the `on_attempt` callback before
its body is parsed and before any retry sleep. That ordering is the hook the
ledger hangs on: the bytes are archived the moment they arrive, whatever the
walk later decides about them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, Protocol
from urllib.parse import parse_qs, parse_qsl, urlsplit

from .transport import Fetch, HeaderPairs, OnAttempt, PageAttempt, Transport, urllib_fetch

BASE = "https://api.pluggy.ai"
PAGE_SIZE = 500
HOP_LIMIT = 500
RETRIES = 3

# Pluggy documents 360 requests/minute per IP; a prior implementation hardcoded
# 360/hour and the disagreement was never settled. Sequential requests with a
# small gap stay far below either reading, and batch processes against the API
# are explicitly prohibited, so there is nothing to gain by going faster.
REQUEST_GAP_SECONDS = 0.15
RETRY_AFTER_FALLBACK_SECONDS = 60
RETRY_AFTER_CAP_SECONDS = 300

# Endpoints some connectors simply do not serve. A refusal here on the first
# page is a fact about the connector, not a failed walk.
UNAVAILABLE_ENDPOINTS = frozenset({"/identity", "/loans", "/investments"})
REFUSAL_STATUSES = frozenset({403, 404})

Outcome = Literal["complete", "incomplete", "unavailable"]
Document = dict[str, Any]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Clock(Protocol):
    def sleep(self, seconds: float) -> None: ...

    def now(self) -> str: ...


class SystemClock:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> str:
        return utcnow()


@dataclass(frozen=True)
class Page:
    """One accepted page: status 200, not truncated, no transport error."""

    attempt_id: Any
    documents: list[Document]
    error: str | None = None
    page: int | None = None
    total_pages: int | None = None
    next: str | None = None


@dataclass(frozen=True)
class FetchResult:
    pages: list[Page]
    outcome: Outcome
    error: str | None = None
    # HTTP status of the last attempt that produced a whole response, 0 when none did.
    status: int = 0

    @property
    def documents(self) -> list[Document]:
        return [document for page in self.pages for document in page.documents]


class AuthFailure(Exception):
    """`/auth` did not yield a key; the walk that needed it ends `incomplete`."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _reject_non_finite(name: str) -> NoReturn:
    raise ValueError(f"non-finite number {name}")


def load_document(body: bytes) -> tuple[Any, str | None]:
    """Strict JSON: a repeated key or a NaN is a page the archive must not trust."""
    try:
        document = json.loads(
            body, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite
        )
    except (ValueError, RecursionError) as error:
        return None, f"invalid JSON: {error}"
    return document, None


def retry_after_seconds(headers: HeaderPairs) -> int:
    value = next((value for name, value in headers if name.lower() == "retry-after"), None)
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return RETRY_AFTER_FALLBACK_SECONDS
    if seconds < 0:
        return RETRY_AFTER_FALLBACK_SECONDS
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _documents_of(body: Any) -> list[Document] | None:
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
        return None
    return results


def _cursor_key(query: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(parse_qsl(query.lstrip("?"), keep_blank_values=True)))


class _WalkEnded(Exception):
    def __init__(self, result: FetchResult) -> None:
        super().__init__(result.outcome)
        self.result = result


@dataclass
class _Walk:
    endpoint: str
    pages: list[Page] = field(default_factory=list)
    status: int = 0

    def complete(self) -> FetchResult:
        return FetchResult(self.pages, "complete", None, self.status)

    def fail(self, error: str) -> NoReturn:
        raise _WalkEnded(FetchResult(self.pages, "incomplete", error, self.status))

    def refused(self, status: int, position: int) -> NoReturn:
        first_page = position == 1 and not self.pages
        if first_page and status in REFUSAL_STATUSES and self.endpoint in UNAVAILABLE_ENDPOINTS:
            raise _WalkEnded(FetchResult([], "unavailable", None, status))
        self.fail(f"HTTP {status} at page {position}")


@dataclass(frozen=True)
class _Terminal:
    """What the retry loop hands back for one logical page."""

    transport: Transport | None
    attempt_id: Any
    error: str | None


class Client:
    """Authenticates, renews on 401, waits out a 429, and reports every attempt."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        on_attempt: OnAttempt,
        fetch: Fetch = urllib_fetch,
        clock: Clock | None = None,
        base: str = BASE,
    ) -> None:
        self._credentials = {"clientId": client_id, "clientSecret": client_secret}
        self._on_attempt = on_attempt
        self._fetch = fetch
        self._clock: Clock = clock or SystemClock()
        self._base = base
        self._api_key: str | None = None

    # -- authentication -----------------------------------------------------

    def _key(self) -> str:
        if self._api_key is None:
            self._api_key = self._authenticate()
        return self._api_key

    def _authenticate(self) -> str:
        body = json.dumps(self._credentials).encode()
        self._clock.sleep(REQUEST_GAP_SECONDS)
        transport = self._fetch(
            "POST", f"{self._base}/auth", {"content-type": "application/json"}, body
        )
        if not transport.usable:
            raise AuthFailure(f"transport: {transport.error or 'no response'}")
        if not 200 <= transport.status < 300:
            raise AuthFailure(f"HTTP {transport.status}")
        document, parse_error = load_document(transport.body_bytes or b"")
        if parse_error is not None:
            raise AuthFailure(parse_error)
        key = document.get("apiKey") if isinstance(document, dict) else None
        if not isinstance(key, str) or not key:
            raise AuthFailure("no apiKey in the response")
        return key

    # -- one logical page ---------------------------------------------------

    def _archive(self, method: str, url: str, transport: Transport) -> Any:
        attempt = PageAttempt(
            method,
            url,
            transport.status,
            transport.headers,
            transport.body_bytes,
            self._clock.now(),
            transport.error,
        )
        return self._on_attempt(attempt)

    def _terminal(self, url: str) -> _Terminal:
        """Bounded retries for one page. Every attempt is archived before the
        loop decides anything about it, the retry sleeps included."""
        last = _Terminal(None, None, "retries exhausted")
        for attempt in range(RETRIES):
            last_try = attempt == RETRIES - 1
            try:
                key = self._key()
            except AuthFailure as failure:
                return _Terminal(None, None, f"auth: {failure}")
            self._clock.sleep(REQUEST_GAP_SECONDS)
            transport = self._fetch("GET", url, {"X-API-KEY": key}, None)
            try:
                attempt_id = self._archive("GET", url, transport)
            except Exception as raised:  # the contract: a raising callback ends the walk
                return _Terminal(None, None, f"on_attempt raised: {raised!r}")
            last = _Terminal(transport, attempt_id, None)
            if not transport.usable:
                if last_try:
                    return _Terminal(transport, attempt_id, f"transport: {transport.error}")
                self._clock.sleep(2**attempt)
                continue
            if transport.status == 401 and not last_try:
                self._api_key = None
                continue
            if transport.status == 429 and not last_try:
                self._clock.sleep(retry_after_seconds(transport.headers))
                continue
            return last
        return last

    def _page_body(self, walk: _Walk, url: str, position: int) -> tuple[Any, Any]:
        terminal = self._terminal(url)
        transport = terminal.transport
        walk.status = transport.status if transport is not None and transport.usable else 0
        if terminal.error is not None or transport is None:
            walk.fail(terminal.error or "no response")
        if transport.status != 200:
            walk.refused(transport.status, position)
        body, parse_error = load_document(transport.body_bytes or b"")
        if parse_error is not None:
            walk.pages.append(Page(terminal.attempt_id, [], parse_error))
            walk.fail(f"page {position}: {parse_error}")
        return body, terminal.attempt_id

    # -- the three walks ----------------------------------------------------

    def single(self, path: str, expected_id: str | None = None) -> FetchResult:
        walk = _Walk(urlsplit(path).path)
        try:
            body, attempt_id = self._page_body(walk, f"{self._base}{path}", 1)
            error = _single_shape_error(body, expected_id)
            walk.pages.append(Page(attempt_id, [] if error else [body], error))
            if error is not None:
                walk.fail(f"document: {error}")
            return walk.complete()
        except _WalkEnded as ended:
            return ended.result

    def paged(self, path: str) -> FetchResult:
        walk = _Walk(urlsplit(path).path)
        joiner = "&" if "?" in path else "?"
        total: int | None = None
        try:
            for page in range(1, HOP_LIMIT + 1):
                url = f"{self._base}{path}{joiner}pageSize={PAGE_SIZE}&page={page}"
                body, attempt_id = self._page_body(walk, url, page)
                error = _paged_shape_error(body, page, total)
                documents = _documents_of(body) or []
                total_pages = body.get("totalPages") if isinstance(body, dict) else None
                answered = body.get("page") if isinstance(body, dict) else None
                walk.pages.append(Page(attempt_id, documents, error, answered, total_pages))
                if error is not None:
                    walk.fail(f"page {page}: {error}")
                if total_pages is None:
                    return walk.complete()
                total = total_pages
                if page >= max(total, 1):
                    return walk.complete()
            walk.fail(f"exceeded {HOP_LIMIT} pages")
        except _WalkEnded as ended:
            return ended.result

    def cursored(self, path: str, query: str) -> FetchResult:
        walk = _Walk(urlsplit(path).path)
        account = parse_qs(query.lstrip("?")).get("accountId")
        visited = {_cursor_key(query)}
        try:
            for hop in range(1, HOP_LIMIT + 1):
                body, attempt_id = self._page_body(walk, f"{self._base}{path}{query}", hop)
                error = _cursored_shape_error(body, path, account, visited)
                documents = _documents_of(body) or []
                following = body.get("next") if isinstance(body, dict) else None
                walk.pages.append(Page(attempt_id, documents, error, next=following))
                if error is not None:
                    walk.fail(f"page {hop}: {error}")
                if following is None:
                    return walk.complete()
                query = f"?{urlsplit(following).query}"
                visited.add(_cursor_key(query))
            walk.fail(f"exceeded {HOP_LIMIT} pages")
        except _WalkEnded as ended:
            return ended.result


def _single_shape_error(body: Any, expected_id: str | None) -> str | None:
    if not isinstance(body, dict):
        return "not an object"
    identifier = body.get("id")
    if not isinstance(identifier, str) or not identifier:
        return "no id"
    if expected_id is not None and identifier != expected_id:
        return f"id {identifier!r} is not the requested {expected_id!r}"
    return None


def _paged_shape_error(body: Any, page: int, total: int | None) -> str | None:
    documents = _documents_of(body)
    if documents is None:
        return "no results list"
    answered = body.get("page")
    if answered is not None and (not _is_count(answered) or answered != page):
        return f"page {answered!r} answered for page {page}"
    total_pages = body.get("totalPages")
    if total_pages is None:
        # What /categories may return: a single page, told apart from a cut
        # walk only because it holds fewer rows than were asked for.
        if page == 1 and len(documents) < PAGE_SIZE:
            return None
        return "no totalPages"
    if not _is_count(total_pages):
        return f"totalPages {total_pages!r} is not a count"
    if total is not None and total_pages != total:
        return f"totalPages changed from {total} to {total_pages}"
    return None


def _cursored_shape_error(
    body: Any, path: str, account: list[str] | None, visited: set[tuple[tuple[str, str], ...]]
) -> str | None:
    """`next` is a query string relative to the endpoint (`?accountId=...&after=...`).
    Sent as a URL of its own it would hit the API root and end the walk early
    with no error, so anything but that form, or a cursor already walked, is
    refused here."""
    if _documents_of(body) is None:
        return "no results list"
    following = body.get("next")
    if following is None:
        return None
    if not isinstance(following, str):
        return f"next {following!r} is not a string"
    parts = urlsplit(following)
    if parts.scheme or parts.netloc or parts.path not in ("", path):
        return f"next {following!r} does not name this endpoint"
    if not following.startswith("?") and not following.startswith(path):
        return f"next {following!r} is not a relative query"
    parameters = parse_qs(parts.query)
    if parameters.get("accountId") != account:
        return f"next {following!r} names another account"
    if _cursor_key(parts.query) in visited:
        return f"next {following!r} does not advance the cursor"
    return None


__all__ = [
    "BASE",
    "AuthFailure",
    "Client",
    "Clock",
    "FetchResult",
    "Page",
    "SystemClock",
    "load_document",
    "retry_after_seconds",
    "utcnow",
]
