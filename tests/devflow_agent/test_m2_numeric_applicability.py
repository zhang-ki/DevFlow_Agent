from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devflow_agent.answer_quality import evaluate_arithmetic_claims
from devflow_agent.real_readonly_agent import validate_answer_citations
from devflow_agent.repository import AuthorizedWorkspace


class StaticNumericApplicabilityTests(unittest.TestCase):
    def _evidence(self, workspace: AuthorizedWorkspace, path: str) -> dict:
        snapshot = workspace.read_text(path)
        return {
            "result_kind": "trusted_file_read",
            "evidence_id": snapshot.evidence_id,
            "workspace_id": snapshot.workspace_id,
            "relative_path": snapshot.relative_path,
            "start_line": snapshot.start_line,
            "end_line": snapshot.end_line,
            "line_count": snapshot.line_count,
            "content": snapshot.content,
            "full_file_sha256": snapshot.full_file_sha256,
            "fragment_sha256": snapshot.fragment_sha256,
        }

    def _workspace(
        self, source: str
    ) -> tuple[tempfile.TemporaryDirectory, AuthorizedWorkspace, dict]:
        temporary = tempfile.TemporaryDirectory(prefix="devflow-static-numeric-")
        root = Path(temporary.name)
        (root / "src").mkdir()
        (root / "src" / "totals.py").write_text(source, encoding="utf-8")
        workspace = AuthorizedWorkspace(root)
        return temporary, workspace, self._evidence(workspace, "src/totals.py")

    def test_supported_arithmetic_remains_strict(self) -> None:
        good = evaluate_arithmetic_claims("sum([-100, 50, -200]) returns -250")
        self.assertEqual(good["status"], "PASS")
        self.assertEqual(good["claims"][0]["applicability"], "SUPPORTED")
        wrong = evaluate_arithmetic_claims("sum([-100, 50, -200]) returns -350")
        self.assertEqual(wrong["status"], "NUMERIC_MISMATCH")

    def test_simple_project_function_is_statically_verifiable(self) -> None:
        temporary, _workspace, evidence = self._workspace(
            "def total(values):\n    return sum(values)\n"
        )
        try:
            result = evaluate_arithmetic_claims(
                "total([-100, 50, -200]) returns -250",
                trusted_reads=[evidence],
            )
            self.assertEqual(result["status"], "PASS")
            claim = result["claims"][0]
            self.assertEqual(claim["applicability"], "STATICALLY_VERIFIABLE")
            self.assertEqual(claim["actual_value"], -250)
            self.assertEqual(claim["source_path"], "src/totals.py")
            self.assertEqual(claim["source_line"], 2)
            wrong = evaluate_arithmetic_claims(
                "total([-100, 50, -200]) returns -350",
                trusted_reads=[evidence],
            )
            self.assertEqual(wrong["status"], "NUMERIC_MISMATCH")
        finally:
            temporary.cleanup()

    def test_simple_scalar_function_is_statically_verifiable(self) -> None:
        temporary, _workspace, evidence = self._workspace(
            "def offset(value):\n    return value + 2\n"
        )
        try:
            result = evaluate_arithmetic_claims(
                "offset(3) returns 5",
                trusted_reads=[evidence],
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(
                result["claims"][0]["applicability"], "STATICALLY_VERIFIABLE"
            )
        finally:
            temporary.cleanup()

    def test_branch_dynamic_and_external_functions_fail_closed(self) -> None:
        sources = (
            "def total(values):\n    if values:\n        return sum(values)\n    return 0\n",
            "def total(values):\n    return helper(values)\n",
            "def total(values):\n    return os.getenv('VALUE')\n",
        )
        for source in sources:
            temporary, _workspace, evidence = self._workspace(source)
            try:
                result = evaluate_arithmetic_claims(
                    "total([-1, 1]) returns 0",
                    trusted_reads=[evidence],
                )
                self.assertEqual(result["status"], "UNVERIFIABLE")
                self.assertEqual(result["claims"][0]["applicability"], "UNSUPPORTED")
            finally:
                temporary.cleanup()

    def test_missing_source_evidence_does_not_pass(self) -> None:
        result = evaluate_arithmetic_claims("total([-1, 1]) returns 0")
        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(
            result["claims"][0]["reason"], "trusted_function_source_missing"
        )

    def test_source_expectation_is_not_treated_as_runtime_behavior(self) -> None:
        temporary, _workspace, evidence = self._workspace(
            "def total(values):\n    return 0\n"
        )
        try:
            result = evaluate_arithmetic_claims(
                "The test expects `total([-1, 1]) == 1`.",
                trusted_reads=[evidence],
            )
            self.assertEqual(result["status"], "UNVERIFIABLE")
            self.assertEqual(
                result["source_expectations"][0]["reason"],
                "trusted_test_expectation_missing_or_ambiguous",
            )
        finally:
            temporary.cleanup()

    def test_no_numeric_claim_is_not_applicable(self) -> None:
        result = evaluate_arithmetic_claims("The helper is in src/totals.py:1-2.")
        self.assertEqual(result["status"], "NOT_APPLICABLE")

    def test_stale_trusted_citation_remains_rejected(self) -> None:
        temporary, workspace, evidence = self._workspace(
            "def total(values):\n    return sum(values)\n"
        )
        try:
            path = Path(temporary.name) / "src" / "totals.py"
            path.write_text("def total(values):\n    return 0\n", encoding="utf-8")
            citation = validate_answer_citations(
                "See src/totals.py:1-2.", [evidence], workspace=workspace
            )
            self.assertFalse(citation["valid"])
            self.assertIn("citation_stale_file_version", citation["errors"])
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
