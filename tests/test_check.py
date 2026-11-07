"""`--check` on temporary databases: one test per corruption, plus lock and mode."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pluggysync.check import check_file
from pluggysync.cli import main
from pluggysync.lock import ProcessLock
from pluggysync.walk import BASE
from tests.fakes import ok
from tests.fixtures.ledger_run import (
    ACCOUNT_A1,
    ACCOUNT_A2,
    ITEM_A,
    ITEM_B,
    LedgerHarness,
    script_two_items,
)
from tests.fixtures.pages import cursor_page, cursor_query


def _drop_all_triggers(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()
    for name, _ in rows:
        connection.execute(f"DROP TRIGGER {name}")
    connection.commit()
    return rows


def _restore_triggers(connection: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    for _, sql in rows:
        connection.execute(sql)
    connection.commit()


class CheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LedgerHarness()
        self.addCleanup(self.harness.close)
        transport = self.harness.transport()
        script_two_items(transport)
        self.run_id = self.harness.run(transport, [ITEM_A, ITEM_B]).execute()
        # A second run so the fixture is multi-run.
        second = self.harness.transport()
        script_two_items(second)
        self.run2 = self.harness.run(second, [ITEM_A, ITEM_B]).execute()
        self.db = self.harness.connection

    def copy_db(self) -> str:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        path = tmp.name
        tmp.close()
        self.db.commit()
        source = self.harness.path
        shutil.copyfile(source, path)
        os.chmod(path, 0o600)
        return path

    def corrupt(self, path: str, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(path)
        try:
            triggers = _drop_all_triggers(connection)
            try:
                connection.execute(sql, params)
                connection.commit()
            finally:
                _restore_triggers(connection, triggers)
        finally:
            connection.close()

    def check_path(self, path: str) -> tuple[int, list[str]]:
        code, problems, _ = check_file(path)
        return code, problems

    def run_cli_check(self, path: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"PLUGGY_DB": path}, clear=False):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--check"])
        return code, err.getvalue() + out.getvalue()


class CleanTest(CheckCase):
    def test_clean_multi_run_fixture_exits_0(self) -> None:
        code, problems = self.check_path(self.harness.path)
        self.assertEqual(code, 0)
        self.assertEqual(problems, [])

    def test_cli_check_exits_0(self) -> None:
        code, _ = self.run_cli_check(self.harness.path)
        self.assertEqual(code, 0)


class CorruptionTest(CheckCase):
    def test_flipped_byte_in_versions_body(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT sha256, body FROM versions LIMIT 1").fetchone()
        finally:
            connection.close()
        document = json.loads(row[1])
        if "amount" in document and isinstance(document["amount"], (int, float)):
            document["amount"] = document["amount"] + 1
        elif "balance" in document and isinstance(document["balance"], (int, float)):
            document["balance"] = document["balance"] + 1
        else:
            document["_check_flip"] = 1
        body = json.dumps(document)
        self.corrupt(path, "UPDATE versions SET body = ? WHERE sha256 = ?", (body, row[0]))
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("version" in p and "canonical" in p for p in problems), problems)

    def test_flipped_byte_in_responses_body(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT id, body_zlib FROM responses WHERE body_zlib IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        blob = bytes(row[1])
        flipped = bytes([blob[0] ^ 1]) + blob[1:]
        self.corrupt(path, "UPDATE responses SET body_zlib = ? WHERE id = ?", (flipped, row[0]))
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any(f"response {row[0]}" in p for p in problems), problems)

    def test_changed_projection_amount(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        self.corrupt(path, "UPDATE transactions SET amount = amount + 1 WHERE id LIKE 'tx-%'")
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("transactions" in p or "layer 3" in p for p in problems), problems)

    def test_scope_rows_off_by_one(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT id, rows FROM scopes WHERE outcome = 'complete' AND rows > 0 LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.corrupt(path, "UPDATE scopes SET rows = rows + 1 WHERE id = ?", (row[0],))
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("rows" in p for p in problems), problems)

    def test_scope_outcome_incomplete_to_complete(self) -> None:
        harness = LedgerHarness()
        self.addCleanup(harness.close)
        transport = harness.transport()
        script_two_items(transport)
        transport.script[f"{BASE}/v2/transactions{cursor_query(ACCOUNT_A1)}"].clear()
        second = cursor_query(ACCOUNT_A1, after="c1")
        from tests.fixtures.ledger_run import tx_doc

        transport.answer(
            f"/v2/transactions{cursor_query(ACCOUNT_A1)}",
            ok(cursor_page([tx_doc("tx-a1-0001", ACCOUNT_A1)], second)),
        )
        transport.answer(
            f"/v2/transactions{second}",
            ok(cursor_page([{"id": "", "amount": 1}])),
        )
        run_id = harness.run(transport, [ITEM_A, ITEM_B]).execute()
        scope_id = harness.connection.execute(
            "SELECT id FROM scopes WHERE run_id = ? AND kind = 'transactions' AND parent = ?",
            (run_id, ACCOUNT_A1),
        ).fetchone()[0]
        import tempfile

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        copied = tmp.name
        tmp.close()
        self.addCleanup(os.unlink, copied)
        shutil.copyfile(harness.path, copied)
        os.chmod(copied, 0o600)
        connection = sqlite3.connect(copied)
        try:
            triggers = _drop_all_triggers(connection)
            try:
                connection.execute(
                    "UPDATE scopes SET outcome = 'complete', infer_absence = 1 WHERE id = ?",
                    (scope_id,),
                )
                connection.commit()
            finally:
                _restore_triggers(connection, triggers)
        finally:
            connection.close()
        code, problems = self.check_path(copied)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("outcome" in p for p in problems), problems)

    def test_observation_pointing_at_another_scopes_response(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        scopes = connection.execute(
            "SELECT id FROM scopes WHERE run_id = ? AND kind = 'transactions' ORDER BY id",
            (self.run_id,),
        ).fetchall()
        self.assertGreaterEqual(len(scopes), 2)
        first, second = scopes[0][0], scopes[1][0]
        response = connection.execute(
            "SELECT id FROM responses WHERE scope_id = ? LIMIT 1", (second,)
        ).fetchone()[0]
        observation = connection.execute(
            "SELECT id FROM observations WHERE scope_id = ? LIMIT 1", (first,)
        ).fetchone()[0]
        connection.close()
        self.corrupt(
            path,
            "UPDATE observations SET response_id = ?, position = 999 WHERE id = ?",
            (response, observation),
        )
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(
            any("response" in p or "rows" in p or "parent" in p for p in problems), problems
        )

    def test_observation_url_names_another_account(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT id, url, scope_id FROM responses WHERE url LIKE '%accountId=%' LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        bad_url = row[1].replace(ACCOUNT_A1, ACCOUNT_A2) if ACCOUNT_A1 in row[1] else row[1] + "x"
        self.corrupt(path, "UPDATE responses SET url = ? WHERE id = ?", (bad_url, row[0]))
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("URL" in p or "url" in p or "scope" in p for p in problems), problems)

    def test_dropped_append_only_trigger(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        connection.execute("DROP TRIGGER observations_no_update")
        connection.commit()
        connection.close()
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("trigger" in p or "missing" in p for p in problems), problems)

    def test_pending_scope_on_terminal_run(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT id FROM scopes WHERE run_id = ? LIMIT 1", (self.run_id,)
            ).fetchone()
        finally:
            connection.close()
        self.corrupt(
            path,
            "UPDATE scopes SET outcome = 'pending', infer_absence = 0 WHERE id = ?",
            (row[0],),
        )
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("pending" in p for p in problems), problems)

    def test_api_calls_off_by_one(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        self.corrupt(path, "UPDATE runs SET api_calls = api_calls + 1 WHERE id = ?", (self.run_id,))
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("api_calls" in p for p in problems), problems)

    def test_file_mode_0644(self) -> None:
        path = self.copy_db()
        self.addCleanup(os.unlink, path)
        os.chmod(path, 0o644)
        code, problems = self.check_path(path)
        self.assertNotEqual(code, 0)
        self.assertTrue(any("0600" in p or "0644" in p or "mode" in p for p in problems), problems)


class LockTest(CheckCase):
    def test_check_during_running_sync_reports_writer_active_and_reads_nothing(self) -> None:
        with open(self.harness.path, "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()
        lock = ProcessLock.exclusive(self.harness.path)
        try:
            code, problems, _ = check_file(self.harness.path)
            self.assertNotEqual(code, 0)
            self.assertTrue(any("writer active" in p for p in problems), problems)
        finally:
            lock.release()
        with open(self.harness.path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
