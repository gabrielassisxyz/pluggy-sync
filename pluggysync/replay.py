"""One walk per scope kind, and the same walk replayed from the archive.

The recovery sweep and the integrity check must reach the verdict the live walk
would have reached from the same attempts. Rather than a second copy of the
walk's rules, the archived responses are served back through the real `Client`
by a transport that answers each request with the next archived attempt. When
the archive runs out, the walk sees a transport failure and ends `incomplete`,
which is exactly what a crash mid-walk left behind.
"""

from __future__ import annotations

from dataclasses import dataclass

from .archive import ArchivedResponse
from .transport import PageAttempt, Transport
from .walk import BASE, Client, FetchResult

NO_FURTHER_ATTEMPT = "no further attempt archived"


@dataclass(frozen=True)
class ScopeRequest:
    walk: str  # single | paged | cursored
    path: str
    query: str = ""
    expected_id: str | None = None


def scope_request(kind: str, parent: str) -> ScopeRequest:
    """How each scope kind is fetched; the one table the live walk, the replay
    and the URL-to-scope check all read."""
    if kind == "categories":
        return ScopeRequest("paged", "/categories")
    if kind == "items":
        return ScopeRequest("single", f"/items/{parent}", expected_id=parent)
    if kind == "identities":
        return ScopeRequest("single", f"/identity?itemId={parent}")
    if kind in ("consents", "investments", "accounts", "loans"):
        return ScopeRequest("paged", f"/{kind}?itemId={parent}")
    if kind == "investment_transactions":
        return ScopeRequest("paged", f"/investments/{parent}/transactions")
    if kind == "transactions":
        return ScopeRequest("cursored", "/v2/transactions", f"?accountId={parent}")
    if kind == "bills":
        return ScopeRequest("paged", f"/bills?accountId={parent}")
    raise ValueError(f"unknown scope kind {kind!r}")


def walk_scope(client: Client, kind: str, parent: str) -> FetchResult:
    request = scope_request(kind, parent)
    if request.walk == "single":
        return client.single(request.path, expected_id=request.expected_id)
    if request.walk == "cursored":
        return client.cursored(request.path, request.query)
    return client.paged(request.path)


class _StillClock:
    """The replay never waits: the retry sleeps already happened."""

    def sleep(self, seconds: float) -> None:
        return

    def now(self) -> str:
        return "1970-01-01T00:00:00+00:00"


class ArchivedTransport:
    """Serves archived attempts in order; a request beyond the archive, or one
    whose URL is not the archived one, is a transport failure."""

    def __init__(self, archived: list[ArchivedResponse]) -> None:
        self._queue = list(archived)
        self.last_served: int | None = None
        self.phantom_requests = 0

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> Transport:
        if method == "POST" and url.endswith("/auth"):
            return Transport(200, [], b'{"apiKey": "replay"}')
        if not self._queue or self._queue[0].url != url:
            self.last_served = None
            self.phantom_requests += 1
            return Transport(0, [], None, NO_FURTHER_ATTEMPT)
        archived = self._queue.pop(0)
        self.last_served = archived.id
        return Transport(archived.status, archived.headers or [], archived.body, archived.error)

    def attempt_id(self, attempt: PageAttempt) -> int | None:
        return self.last_served


def replay_walk(kind: str, parent: str, archived: list[ArchivedResponse]) -> FetchResult:
    """The verdict the live walk reached, or would have reached, from these
    attempts. Pages carry the archived response id as `attempt_id`."""
    transport = ArchivedTransport(archived)
    client = Client("", "", transport.attempt_id, fetch=transport, clock=_StillClock(), base=BASE)
    return walk_scope(client, kind, parent)
