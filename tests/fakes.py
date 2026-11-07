"""The API faked at the transport boundary, and a clock that only pretends to wait."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pluggysync.transport import PageAttempt, Transport
from pluggysync.walk import BASE

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Sleeping advances `t`; nothing actually waits."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self) -> str:
        return (EPOCH + timedelta(seconds=self.t)).isoformat(timespec="seconds")


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    at: float


@dataclass
class FakeTransport:
    """Answers each URL from a scripted queue, in the order the queue was built.

    A URL with no scripted answer left fails the test loudly instead of
    inventing a page. `/auth` answers with a fresh key each time unless a
    failure is scripted for it.
    """

    clock: FakeClock
    script: dict[str, deque[Transport]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    keys_issued: int = 0

    def answer(self, path: str, *responses: Transport) -> None:
        self.script.setdefault(f"{BASE}{path}", deque()).extend(responses)

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> Transport:
        self.requests.append(Request(method, url, dict(headers), body, self.clock.t))
        queued = self.script.get(url)
        if queued:
            return queued.popleft()
        if url == f"{BASE}/auth":
            self.keys_issued += 1
            return ok({"apiKey": f"key-{self.keys_issued}"})
        raise AssertionError(f"unscripted request: {method} {url}")

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    @property
    def api_keys_sent(self) -> list[str]:
        return [r.headers.get("X-API-KEY", "") for r in self.requests if r.method == "GET"]


class RecordingArchive:
    """The `on_attempt` callback: keeps every attempt and numbers them."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.attempts: list[PageAttempt] = []
        self.sleeps_seen: list[list[float]] = []
        self._clock = clock

    def __call__(self, attempt: PageAttempt) -> int:
        self.attempts.append(attempt)
        if self._clock is not None:
            self.sleeps_seen.append(list(self._clock.sleeps))
        return len(self.attempts)


class RaisingArchive:
    """A callback that fails on the first attempt, the way a full disk would."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, attempt: PageAttempt) -> int:
        self.calls += 1
        raise OSError("archive refused the write")


def ok(document: Any, headers: list[list[str]] | None = None) -> Transport:
    return Transport(200, headers or [["content-type", "application/json"]], encode(document))


def raw(status: int, body: bytes, headers: list[list[str]] | None = None) -> Transport:
    return Transport(status, headers or [], body)


def refused(status: int, headers: list[list[str]] | None = None) -> Transport:
    return Transport(status, headers or [], encode({"message": f"refused with {status}"}))


def unreachable(reason: str = "URLError: connection refused") -> Transport:
    return Transport(0, [], None, reason)


def truncated(partial: bytes) -> Transport:
    return Transport(200, [["content-length", "4096"]], partial, "truncated")


def encode(document: Any) -> bytes:
    return json.dumps(document).encode()
