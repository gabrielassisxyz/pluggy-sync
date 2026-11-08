"""The one behaviour that needs no network: a missing configuration is refused before
anything is written, with exit code 2 and a message naming the variables."""

import io
import os
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from pluggysync.cli import main
from pluggysync.run import code_version

ROOT = pathlib.Path(__file__).resolve().parents[1]


class MissingConfigurationTest(unittest.TestCase):
    def test_missing_variables_exit_2_and_are_named(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGGY_")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stderr(err):
            code = main([])
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
            main([])
        self.assertFalse(target.exists())
        self.assertFalse(pathlib.Path(str(target) + ".lock").exists())


class CodeVersionTest(unittest.TestCase):
    def test_set_returns_override_without_starting_a_subprocess(self) -> None:
        def fail(*args: object, **kwargs: object) -> object:
            raise AssertionError("code_version must not start a subprocess when overridden")

        with mock.patch.dict(os.environ, {"PLUGGY_CODE_VERSION": "v0.0.0-abc"}):
            with mock.patch("pluggysync.run.subprocess.run", side_effect=fail):
                self.assertEqual(code_version(str(ROOT)), "v0.0.0-abc")

    def test_empty_falls_through_to_git(self) -> None:
        described = subprocess.CompletedProcess(
            args=["git", "describe"], returncode=0, stdout="deadbee\n"
        )
        with mock.patch.dict(os.environ, {"PLUGGY_CODE_VERSION": ""}):
            with mock.patch("pluggysync.run.subprocess.run", return_value=described) as run_mock:
                self.assertEqual(code_version(str(ROOT)), "deadbee")
                run_mock.assert_called_once()

    def test_unset_with_git_returns_describe_output(self) -> None:
        described = subprocess.CompletedProcess(
            args=["git", "describe"], returncode=0, stdout="v1.2.3-4-gdeadbee\n"
        )
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("PLUGGY_CODE_VERSION", None)
            with mock.patch("pluggysync.run.subprocess.run", return_value=described):
                self.assertEqual(code_version(str(ROOT)), "v1.2.3-4-gdeadbee")

    def test_unset_without_git_on_path_returns_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with mock.patch.dict(os.environ, {"PATH": empty}):
                os.environ.pop("PLUGGY_CODE_VERSION", None)
                self.assertEqual(code_version(str(ROOT)), "unknown")


if __name__ == "__main__":
    unittest.main()
