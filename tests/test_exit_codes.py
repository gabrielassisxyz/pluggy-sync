"""What the process says about a run: the exit code and the stderr summary,
driven through the command line with a fake transport."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from pluggysync.cli import main
from pluggysync.replay import ArchivedTransport
from pluggysync.walk import BASE, Client
from tests.fakes import FakeClock, FakeTransport, ok, refused
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ITEM_A,
    ITEM_B,
    PAGE_1,
    consent_doc,
    item_doc,
    script_two_items,
)
from tests.fixtures.pages import cursor_query, paged_page


class ExitCodeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.candidate = os.path.join(self.directory.name, "candidate.db")
        self.clock = FakeClock()
        self.clock.t = 12 * 3600
        self.transport = FakeTransport(self.clock)
        script_two_items(self.transport)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        env.update(
            PLUGGY_DB=os.path.join(self.directory.name, "live.db"),
            PLUGGY_CLIENT_ID="placeholder",
            PLUGGY_CLIENT_SECRET="placeholder",
            PLUGGY_ITEM_IDS=f"{ITEM_A},{ITEM_B}",
        )
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.crash_at = None

    def make_client(self, on_attempt) -> Client:
        return Client("placeholder", "placeholder", on_attempt, self.transport, self.clock)

    def run_cli(self) -> tuple[int, str, list[str]]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["--candidate", self.candidate], make_client=self.make_client, clock=self.clock
            )
        return code, out.getvalue(), [line for line in err.getvalue().splitlines() if line]

    def item_at(self, hours_ago: float, **extra) -> dict:
        now = datetime.fromisoformat(self.clock.now())
        body = item_doc(ITEM_A, (now - timedelta(hours=hours_ago)).isoformat())
        body.update(extra)
        return body

    def note_of_last_run(self) -> str | None:
        db = sqlite3.connect(f"file:{self.candidate}?mode=ro", uri=True)
        try:
            return db.execute("SELECT note FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            db.close()


class CompleteRunTest(ExitCodeCase):
    def test_complete_and_fresh_exits_0_with_the_outcome_line_only(self) -> None:
        code, _, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(err, ["run 1: complete; exit 0"])
        self.assertIsNone(self.note_of_last_run())

    def test_an_unavailable_identity_is_still_complete(self) -> None:
        self.transport.script[f"{BASE}/identity?itemId={ITEM_A}"].clear()
        self.transport.answer(f"/identity?itemId={ITEM_A}", refused(404))

        code, _, err = self.run_cli()

        self.assertEqual(code, 0)
        self.assertEqual(err, ["run 1: complete; exit 0"])


class PartialRunTest(ExitCodeCase):
    def test_an_incomplete_owed_walk_exits_1_and_is_named(self) -> None:
        self.transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        self.transport.answer(f"/v2/transactions{cursor_query(ACCOUNT_A1)}", refused(500))

        code, _, err = self.run_cli()

        self.assertEqual(code, 1)
        self.assertEqual(err[0], f"transactions {ACCOUNT_A1} incomplete: HTTP 500 at page 1")
        self.assertEqual(err[-1], "run 1: partial; exit 1")

    def test_a_connection_49_hours_old_is_stale_and_47_is_not(self) -> None:
        for hours, expected_code in ((49, 1), (47, 0)):
            with self.subTest(hours=hours):
                os.unlink(self.candidate) if os.path.exists(self.candidate) else None
                self.transport = FakeTransport(self.clock)
                script_two_items(self.transport)
                self.transport.script[f"{BASE}/items/{ITEM_A}"].clear()
                self.transport.answer(f"/items/{ITEM_A}", ok(self.item_at(hours)))

                code, _, err = self.run_cli()

                self.assertEqual(code, expected_code, err)
                if expected_code == 1:
                    self.assertTrue(
                        any(f"stale item {ITEM_A}: lastUpdatedAt 49h old" in e for e in err), err
                    )
                    self.assertIn("49h old", self.note_of_last_run() or "")

    def test_a_login_error_is_stale(self) -> None:
        self.transport.script[f"{BASE}/items/{ITEM_A}"].clear()
        self.transport.answer(f"/items/{ITEM_A}", ok(self.item_at(1, status="LOGIN_ERROR")))

        code, _, err = self.run_cli()

        self.assertEqual(code, 1)
        self.assertIn(f"stale item {ITEM_A}: status 'LOGIN_ERROR'", err)
        self.assertIn("status 'LOGIN_ERROR'", self.note_of_last_run() or "")

    def test_a_consent_expiring_in_29_days_is_stale_and_31_is_not(self) -> None:
        for days, expected_code in ((29, 1), (31, 0)):
            with self.subTest(days=days):
                os.unlink(self.candidate) if os.path.exists(self.candidate) else None
                self.transport = FakeTransport(self.clock)
                script_two_items(self.transport)
                now = datetime.fromisoformat(self.clock.now())
                expires = (now + timedelta(days=days)).isoformat()
                self.transport.script[f"{BASE}/consents?itemId={ITEM_A}&{PAGE_1}"].clear()
                self.transport.answer(
                    f"/consents?itemId={ITEM_A}&{PAGE_1}",
                    ok(paged_page([consent_doc(ITEM_A, expires)])),
                )

                code, _, err = self.run_cli()

                self.assertEqual(code, expected_code, err)
                if expected_code == 1:
                    self.assertTrue(
                        any("stale consent" in e and "in 29 days" in e for e in err), err
                    )


class FailedRunTest(ExitCodeCase):
    def test_missing_configuration_exits_2_before_any_write(self) -> None:
        del os.environ["PLUGGY_CLIENT_ID"]

        code, _, err = self.run_cli()

        self.assertEqual(code, 2)
        self.assertIn("PLUGGY_CLIENT_ID", " ".join(err))
        self.assertFalse(os.path.exists(self.candidate))

    def test_refused_credentials_fail_the_run_with_exit_3(self) -> None:
        self.transport.answer("/auth", refused(401))

        code, _, err = self.run_cli()

        self.assertEqual(code, 3)
        self.assertEqual(err[-1], "run 1: failed; exit 3")
        self.assertIn("authentication refused", self.note_of_last_run() or "")

    def test_an_unhandled_exception_fails_the_run_with_exit_3(self) -> None:
        original = Client.paged

        def explode(client, path):
            # Only the live client explodes; the recovery replay must stay able to
            # close the scopes the failure left pending.
            if path.startswith("/accounts") and not isinstance(client._fetch, ArchivedTransport):
                raise RuntimeError("injected")
            return original(client, path)

        with mock.patch.object(Client, "paged", explode):
            code, _, err = self.run_cli()

        self.assertEqual(code, 3)
        self.assertEqual(err[-1], "run 1: failed; exit 3")
        self.assertIn("injected", self.note_of_last_run() or "")


if __name__ == "__main__":
    unittest.main()
