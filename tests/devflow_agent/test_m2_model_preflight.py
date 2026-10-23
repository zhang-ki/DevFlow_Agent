from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.m1_5_http_support import check_model_profile, check_pytest_baseline


class ModelProfilePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _project(self, config_text: str | None) -> Path:
        root = Path(self._temp.name)
        if config_text is not None:
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(config_text, encoding="utf-8")
        return root

    def test_success_returns_only_allow_listed_profile_fields(self) -> None:
        secret = "should-never-appear"
        root = self._project(
            """
[llm]
model = "test-model"
base_url = "http://127.0.0.1:11434/v1"
api_type = "openai"
api_key = "should-never-appear"
max_tokens = 321
""".strip()
        )

        result = check_model_profile(root)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["effective_config_max_tokens"], 321)
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(str(root), repr(result))

    def test_missing_config_is_structured_and_redacted(self) -> None:
        root = self._project(None)

        result = check_model_profile(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error_code"], "local_model_config_missing")
        self.assertEqual(result["failure_stage"], "load_profile")
        self.assertEqual(result["exception_type"], "ReadonlyAgentError")
        self.assertNotIn(str(root), repr(result))

    def test_parse_failure_does_not_echo_private_config(self) -> None:
        secret = "private-token-value"
        root = self._project(f'[llm\napi_key = "{secret}"')

        result = check_model_profile(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error_code"], "invalid_model_config")
        self.assertEqual(result["failure_stage"], "load_profile")
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(str(root), repr(result))

    def test_unexpected_exception_is_redacted(self) -> None:
        secret = "secret-from-unexpected-error"
        root = self._project("[llm]\nmodel = 'test'\n")

        with patch(
            "devflow_agent.real_readonly_agent.load_local_model_profile",
            side_effect=RuntimeError(f"{secret}: {root}"),
        ):
            result = check_model_profile(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error_code"], "unexpected_preflight_error")
        self.assertEqual(result["exception_type"], "RuntimeError")
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(str(root), repr(result))


class SyntheticPytestBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._temp.name)
        self.log = self.run_dir / "baseline-pytest.log"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _check(self, *, exit_code: int = 1) -> dict[str, object]:
        return check_pytest_baseline(
            self.run_dir,
            self.log,
            exit_code,
            "tests/test_totals.py::test_total",
            4,
            3,
        )

    def test_real_windows_pytest_shape_is_accepted(self) -> None:
        self.log.write_text(
            "E       assert 4 == 3\n"
            "tests\\test_totals.py:4: AssertionError\n"
            "FAILED tests\\test_totals.py::test_total - assert 4 == 3\n"
            "1 failed in 0.11s\n",
            encoding="utf-8",
        )

        result = self._check()

        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["assertion_match"])
        self.assertIn("log_sha256", result)

    def test_assertion_without_failed_nodeid_is_rejected(self) -> None:
        self.log.write_text(
            "E       assert 4 == 3\n1 failed in 0.11s\n", encoding="utf-8"
        )

        result = self._check()

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error_code"], "baseline_evidence_mismatch")
        self.assertFalse(result["nodeid_match"])

    def test_wrong_exit_code_or_assertion_is_rejected(self) -> None:
        self.log.write_text(
            "E       assert 5 == 3\n"
            "FAILED tests/test_totals.py::test_total - assert 5 == 3\n"
            "1 failed in 0.11s\n",
            encoding="utf-8",
        )

        result = self._check(exit_code=0)

        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["exit_code_match"])
        self.assertFalse(result["assertion_match"])

    def test_log_outside_run_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            outside_log = Path(outside) / "pytest.log"
            outside_log.write_text("E       assert 4 == 3\n", encoding="utf-8")

            result = check_pytest_baseline(
                self.run_dir,
                outside_log,
                1,
                "tests/test_totals.py::test_total",
                4,
                3,
            )

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error_code"], "baseline_log_outside_run")


if __name__ == "__main__":
    unittest.main()
