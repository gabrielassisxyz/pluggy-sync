"""The HTTP boundary: a request goes in, bytes come out, nothing is parsed here.

The ledger that follows needs the exact bytes of every attempt, including the
ones that failed, so the transport reports what arrived rather than what it
meant. A transport function never raises: every failure is a `Transport` value
whose `error` says what went wrong, and the caller decides whether to retry.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Response headers travel as ordered pairs rather than a dict: the archive keeps
# them verbatim, and a dict would silently drop a repeated header.
HeaderPairs = list[list[str]]

READ_CHUNK_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Transport:
    """What one HTTP attempt produced.

    `status` is 0 and `body_bytes` is None when no status line arrived. A read
    that failed mid-body keeps the bytes received so far with `error` set to
    `truncated`; such a body is never parsed as a page.
    """

    status: int
    headers: HeaderPairs
    body_bytes: bytes | None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.status > 0 and self.error is None and self.body_bytes is not None


@dataclass(frozen=True)
class PageAttempt:
    """One attempt as handed to the `on_attempt` callback, before parsing."""

    method: str
    url: str
    status: int
    headers: HeaderPairs
    body_bytes: bytes | None
    received_at: str
    error: str | None = None


Fetch = Callable[[str, str, dict[str, str], bytes | None], Transport]
OnAttempt = Callable[[PageAttempt], Any]


class _RedirectIsAnAnswer(urllib.request.HTTPRedirectHandler):
    """`urllib` would follow a 3xx and resend every header, API key included, to
    whatever host `Location` names, and the archive would file the other host's
    bytes under the URL that was asked. A redirect is reported as the status it is."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_RedirectIsAnAnswer)


def _read_everything(stream: Any, headers: HeaderPairs) -> tuple[bytes, str | None]:
    """Reads a response body chunk by chunk so a failure mid-body still returns
    what had arrived, instead of losing the whole response to one exception.

    `http.client` deliberately does not raise when a sized read ends short of
    `Content-Length` (a compatibility choice noted in its source), so the
    declared length is checked here: fewer bytes than promised is a truncation.
    """
    received = bytearray()
    try:
        while True:
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            received += chunk
    except http.client.IncompleteRead as error:
        received += error.partial
        return bytes(received), "truncated"
    except (OSError, http.client.HTTPException):
        return bytes(received), "truncated"
    declared = _declared_length(headers)
    if declared is not None and len(received) < declared:
        return bytes(received), "truncated"
    return bytes(received), None


def _declared_length(headers: HeaderPairs) -> int | None:
    value = next((value for name, value in headers if name.lower() == "content-length"), None)
    try:
        return int(str(value).strip()) if value is not None else None
    except ValueError:
        return None


def _header_pairs(message: Any) -> HeaderPairs:
    return [[name, value] for name, value in message.items()]


def urllib_fetch(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transport:
    """The real transport, over `urllib`. Never raises."""
    try:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # A non-2xx answer is still an answer: status, headers and body arrived.
        headers_received = _header_pairs(error.headers)
        with error:
            received, read_error = _read_everything(error, headers_received)
        return Transport(error.code, headers_received, received, read_error)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        return Transport(0, [], None, _describe(error))
    headers_received = _header_pairs(response.headers)
    with response:
        received, read_error = _read_everything(response, headers_received)
    return Transport(response.status, headers_received, received, read_error)


def _describe(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    text = str(reason) if reason is not None else str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__
