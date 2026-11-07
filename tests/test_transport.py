"""The real transport against a loopback server: what arrives is what is
reported, and nothing raises. No request leaves the machine."""

from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from pluggysync.transport import urllib_fetch


class ScriptedHandler(BaseHTTPRequestHandler):
    """Each path is one scenario; the body never contains a real value."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:  # keep the test output clean
        return

    def do_GET(self) -> None:  # the name is the http.server contract
        if self.path == "/ok":
            self._reply(200, b'{"results": []}', [("x-repeated", "one"), ("x-repeated", "two")])
        elif self.path == "/refused":
            self._reply(404, b'{"message": "not here"}')
        elif self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "4096")
            self.end_headers()
            self.wfile.write(b'{"results": [{"id": "')
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
        else:
            self._reply(500, b"")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        received = self.rfile.read(length)
        self._reply(200, b'{"echo": ' + str(len(received)).encode() + b"}")

    def _reply(self, status: int, body: bytes, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class UrllibFetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), ScriptedHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_200_returns_bytes_and_ordered_header_pairs_with_repeats(self) -> None:
        transport = urllib_fetch("GET", f"{self.base}/ok", {"X-API-KEY": "k"})

        self.assertEqual(transport.status, 200)
        self.assertEqual(transport.body_bytes, b'{"results": []}')
        self.assertIsNone(transport.error)
        repeated = [value for name, value in transport.headers if name.lower() == "x-repeated"]
        self.assertEqual(repeated, ["one", "two"])
        self.assertTrue(
            all(isinstance(pair, list) and len(pair) == 2 for pair in transport.headers)
        )

    def test_non_2xx_is_a_value_with_its_body(self) -> None:
        transport = urllib_fetch("GET", f"{self.base}/refused", {})

        self.assertEqual(transport.status, 404)
        self.assertEqual(transport.body_bytes, b'{"message": "not here"}')
        self.assertIsNone(transport.error)
        self.assertTrue(transport.usable)

    def test_body_cut_mid_read_keeps_the_bytes_received_and_is_marked_truncated(self) -> None:
        transport = urllib_fetch("GET", f"{self.base}/truncated", {}, timeout=5)

        self.assertEqual(transport.status, 200)
        self.assertEqual(transport.error, "truncated")
        self.assertEqual(transport.body_bytes, b'{"results": [{"id": "')
        self.assertFalse(transport.usable)

    def test_no_listener_is_status_0_with_no_body(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]

        transport = urllib_fetch("GET", f"http://127.0.0.1:{closed_port}/", {}, timeout=5)

        self.assertEqual(transport.status, 0)
        self.assertIsNone(transport.body_bytes)
        self.assertIsNotNone(transport.error)
        self.assertFalse(transport.usable)

    def test_post_sends_the_body(self) -> None:
        transport = urllib_fetch("POST", f"{self.base}/auth", {}, b'{"clientId": "x"}')

        self.assertEqual(transport.status, 200)
        self.assertEqual(transport.body_bytes, b'{"echo": 17}')


if __name__ == "__main__":
    unittest.main()
