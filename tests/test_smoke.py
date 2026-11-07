"""The one behaviour that needs no network: a missing configuration is refused before
anything is written, with exit code 2 and a message naming the variables."""

import io
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pluggy_sync  # noqa: E402


class MissingConfigurationTest(unittest.TestCase):
    def test_missing_variables_exit_2_and_are_named(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(err):
            code = pluggy_sync.sync()
        self.assertEqual(code, 2)
        for name in ("PLUGGY_CLIENT_ID", "PLUGGY_CLIENT_SECRET", "PLUGGY_ITEM_IDS"):
            self.assertIn(name, err.getvalue())

    def test_no_database_is_created_without_configuration(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        target = ROOT / "tests" / "should-not-exist.db"
        env["PLUGGY_DB"] = str(target)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            redirect_stderr(io.StringIO()),
        ):
            pluggy_sync.sync()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
