from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from devflow_agent.diagnostics import (
    diagnose_failures,
    evaluate_diagnostic_case,
    parse_pytest_log,
)
from devflow_agent.repository import AuthorizedWorkspace


class M2DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2]
        cls.cases = [
            json.loads(line)
            for line in (root / "evals" / "m2_ci_failures.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    def _workspace_for(
        self, case: dict
    ) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="devflow-m2-diagnostic-")
        root = Path(temporary.name)
        test_path = case["expected_test_nodeid"].split("::", 1)[0]
        test_file = root / test_path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text(
            "def test_case():\n    assert True\n    assert True\n    assert True\n    assert True\n    assert True\n    assert True\n    assert True\n    assert True\n",
            encoding="utf-8",
        )
        source_path = case.get("expected_source_path")
        if source_path and case["id"] != "misleading_log":
            source_file = root / source_path
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                "def implementation(value):\n    return value\n    return value\n    return value\n    return value\n    return value\n    return value\n    return value\n    return value\n",
                encoding="utf-8",
            )
        return temporary, root

    def test_fixed_cases_parse_and_localize_with_evidence(self) -> None:
        self.assertEqual(len(self.cases), 5)
        for case in self.cases:
            with self.subTest(case=case["id"]):
                parsed = parse_pytest_log(case["log"], exit_code=1)
                self.assertEqual(len(parsed.failures), 1)
                self.assertEqual(
                    parsed.failures[0].nodeid, case["expected_test_nodeid"]
                )
                temporary, root = self._workspace_for(case)
                try:
                    report = diagnose_failures(
                        AuthorizedWorkspace(root), case["log"], exit_code=1
                    )
                    score = evaluate_diagnostic_case(report, case)
                    self.assertTrue(score["test_localized"])
                    self.assertTrue(score["exception_classified"])
                    if case["id"] == "misleading_log":
                        self.assertEqual(report.status, "PARTIAL")
                        self.assertFalse(score["file_top1"])
                    else:
                        self.assertEqual(report.status, "PASS")
                        self.assertTrue(score["file_top1"], report.to_dict())
                        self.assertIn(
                            case["expected_source_path"], score["evidence_paths"]
                        )
                finally:
                    temporary.cleanup()

    def test_diagnostic_report_has_bounded_read_only_contract(self) -> None:
        case = self.cases[0]
        temporary, root = self._workspace_for(case)
        try:
            workspace = AuthorizedWorkspace(root)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            report = diagnose_failures(workspace, case["log"], exit_code=1)
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            payload = report.to_dict()
            self.assertTrue(payload["read_only"])
            self.assertFalse(payload["commands_executed"])
            self.assertEqual(payload["metrics"]["commands_executed"], 0)
            self.assertEqual(
                payload["metrics"]["read_only_tools"],
                ["repository_search_readonly", "repository_readonly_read"],
            )
            self.assertIn("# DevFlow CI failure diagnosis", report.to_markdown())
        finally:
            temporary.cleanup()

    def test_absolute_log_path_cannot_escape_as_source_evidence(self) -> None:
        log = (
            "FAILED tests/test_x.py::test_x - AssertionError\n"
            "E   AssertionError: failed\n"
            "FAILED C:/outside/secret.py::test_x - AssertionError\n"
        )
        with tempfile.TemporaryDirectory(prefix="devflow-m2-escape-") as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            (root / "tests" / "test_x.py").write_text(
                "def test_x():\n    assert False\n", encoding="utf-8"
            )
            report = diagnose_failures(AuthorizedWorkspace(root), log, exit_code=1)
            self.assertTrue(report.status in {"PASS", "PARTIAL", "UNVERIFIABLE"})
            for failure in report.parsed_failures:
                for evidence in failure.get("evidence", []):
                    self.assertFalse(Path(evidence["relative_path"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
