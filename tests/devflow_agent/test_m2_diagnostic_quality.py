from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devflow_agent.answer_quality import (
    evaluate_answer_quality,
    evaluate_arithmetic_claims,
    evaluate_pytest_diagnostic_evidence,
)
from devflow_agent.real_readonly_agent import validate_answer_citations
from devflow_agent.repository import AuthorizedWorkspace


QUESTION = (
    "Diagnose this captured pytest failure using the trusted repository evidence."
)


def _read_evidence(workspace: AuthorizedWorkspace, path: str) -> dict:
    result = workspace.read_text(path)
    return {
        "result_kind": "trusted_file_read",
        "evidence_id": result.evidence_id,
        "workspace_id": result.workspace_id,
        "relative_path": result.relative_path,
        "start_line": result.start_line,
        "end_line": result.end_line,
        "line_count": result.line_count,
        "content": result.content,
        "full_file_sha256": result.full_file_sha256,
        "fragment_sha256": result.fragment_sha256,
    }


class DiagnosticNumericEvidenceTests(unittest.TestCase):
    def _workspace(self, *, include_log: bool = True):
        temporary = tempfile.TemporaryDirectory(prefix="devflow-m2-quality-")
        root = Path(temporary.name)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "totals.py").write_text(
            "def total(values):\n    return sum(values) + 1\n", encoding="utf-8"
        )
        (root / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\n"
            "def test_total():\n"
            "    assert total([1, 2]) == sum([1, 2])\n",
            encoding="utf-8",
        )
        if include_log:
            (root / "failure.txt").write_text(
                "============================= FAILURES =============================\n"
                "_______________________________ test_total _______________________________\n"
                "tests\\test_totals.py:4: AssertionError\n"
                "    assert total([1, 2]) == sum([1, 2])\n"
                "src/totals.py:2: in total\n"
                "    return sum(values) + 1\n"
                "E       assert 4 == 3\n"
                "FAILED tests/test_totals.py::test_total - assert 4 == 3\n",
                encoding="utf-8",
            )
        workspace = AuthorizedWorkspace(root)
        paths = ["src/totals.py", "tests/test_totals.py"]
        if include_log:
            paths.append("failure.txt")
        evidence = [_read_evidence(workspace, path) for path in paths]
        return temporary, root, workspace, evidence

    @staticmethod
    def _answer(workspace: AuthorizedWorkspace, *, actual: int = 4) -> str:
        lines = [
            "Summary: The captured assertion failed.",
            "Diagnosis class: VERIFIED_ASSERTION_MISMATCH",
            f"Observed result: actual={actual}; expected=3",
            "Evidence:",
        ]
        for path, start, end in (
            ("src/totals.py", 1, 2),
            ("tests/test_totals.py", 1, 4),
            ("failure.txt", 1, 8),
        ):
            evidence = workspace.read_text(path, start_line=start, end_line=end)
            lines.append(
                f"- {path}:{start}-{end} (SHA-256: {evidence.full_file_sha256})"
            )
        lines.extend(
            [
                "Confidence: high for the observed assertion mismatch.",
                "Unconfirmed: the verifier does not infer behavior outside this assertion.",
            ]
        )
        return "\n".join(lines)

    def test_test_expectation_is_verified_as_source_fact_not_runtime_result(
        self,
    ) -> None:
        temporary, _root, _workspace, evidence = self._workspace(include_log=False)
        try:
            checked = evaluate_arithmetic_claims(
                "The test expects `total([1, 2]) == 3`.", trusted_reads=evidence
            )
            self.assertEqual(checked["status"], "NOT_APPLICABLE")
            self.assertEqual(checked["claims_checked"], 0)
            expectation = checked["source_expectations"][0]
            self.assertEqual(expectation["classification"], "VERIFIED_SOURCE_OR_TEST")
            self.assertEqual(expectation["test_expected_value"], 3)
            self.assertEqual(expectation["source_static_value"], 4)
            self.assertFalse(expectation["matches_source_static_value"])
            self.assertEqual(
                expectation["runtime_observation"],
                "NOT_INFERRED_FROM_EXPECTATION",
            )
        finally:
            temporary.cleanup()

    def test_saved_answer_without_trusted_pytest_log_stays_unverifiable(self) -> None:
        temporary, _root, workspace, evidence = self._workspace(include_log=False)
        try:
            answer = (
                "Summary: The implementation is broken.\n"
                "Root cause: The function returns zero.\n"
                "Evidence:\n"
                "- src/totals.py:1-2\n"
                "- tests/test_totals.py:3-4\n"
                "Test expects `total([1, 2]) == 3`.\n"
                "Confidence: High\nUnconfirmed: None"
            )
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "trusted_pytest_failure_log_missing_or_ambiguous",
                diagnostic["errors"],
            )
            self.assertTrue(
                any(
                    claim.get("classification") == "VERIFIED_SOURCE_OR_TEST"
                    for claim in diagnostic["claims"]
                )
            )
            quality = evaluate_answer_quality(
                answer=answer,
                run_status="completed",
                terminal_reason="final_answer",
                trace_issues=(),
                read_evidence_count=len(evidence),
                citation_check=citations,
                numeric_check=evaluate_arithmetic_claims(
                    answer, trusted_reads=evidence
                ),
                diagnostic_check=diagnostic,
            )
            self.assertEqual(quality["status"], "UNVERIFIABLE")
        finally:
            temporary.cleanup()

    def test_consistent_pytest_source_test_and_answer_pass_narrow_gate(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            self.assertTrue(citations["valid"], citations)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "PASS", diagnostic)
            self.assertEqual(
                diagnostic["verified_conclusion"], "ASSERTION_MISMATCH_ONLY"
            )
            self.assertEqual(
                diagnostic["intended_contract"], "NOT_PROVEN_BY_THIS_CHECK"
            )
            self.assertEqual(diagnostic["claims"][-2]["line"], 2)
            self.assertEqual(
                [claim["classification"] for claim in diagnostic["claims"][-4:]],
                [
                    "VERIFIED_SOURCE_OR_TEST",
                    "VERIFIED_SOURCE_OR_TEST",
                    "VERIFIED_SOURCE_OR_TEST",
                    "VERIFIED_SOURCE_OR_TEST",
                ],
            )
            quality = evaluate_answer_quality(
                answer=answer,
                run_status="completed",
                terminal_reason="final_answer",
                trace_issues=(),
                read_evidence_count=len(evidence),
                citation_check=citations,
                numeric_check=evaluate_arithmetic_claims(
                    answer, trusted_reads=evidence
                ),
                diagnostic_check=diagnostic,
            )
            self.assertEqual(quality["status"], "PASS")
        finally:
            temporary.cleanup()

    def test_diagnostic_accepts_a_one_pass_trusted_read_iterator(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, iter(evidence), citations
            )
            self.assertEqual(diagnostic["status"], "PASS", diagnostic)
        finally:
            temporary.cleanup()

    def test_answer_values_must_match_observed_pytest_values(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace, actual=3)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "DIAGNOSTIC_MISMATCH")
            self.assertIn(
                "answer_observed_result_does_not_match_pytest_log",
                diagnostic["errors"],
            )
        finally:
            temporary.cleanup()

    def test_pytest_assertion_is_not_verified_without_citing_the_log(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace).replace(
                "- failure.txt:1-8 (SHA-256: "
                + workspace.read_text("failure.txt").full_file_sha256
                + ")\n",
                "",
            )
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            self.assertTrue(citations["valid"], citations)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "diagnostic_evidence_lines_not_all_cited", diagnostic["errors"]
            )
        finally:
            temporary.cleanup()

    def test_definition_line_alone_does_not_cite_return_behavior(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace).replace(
                "src/totals.py:1-2", "src/totals.py:1"
            )
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            self.assertTrue(citations["valid"], citations)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "diagnostic_evidence_lines_not_all_cited", diagnostic["errors"]
            )
        finally:
            temporary.cleanup()

    def test_pytest_frame_must_point_to_the_static_test_assertion(self) -> None:
        temporary, root, workspace, _evidence = self._workspace()
        try:
            log_path = root / "failure.txt"
            log_path.write_text(
                log_path.read_text(encoding="utf-8").replace(
                    "tests\\test_totals.py:4: AssertionError",
                    "tests\\test_totals.py:3: AssertionError",
                ),
                encoding="utf-8",
            )
            evidence = [
                _read_evidence(workspace, path)
                for path in ("src/totals.py", "tests/test_totals.py", "failure.txt")
            ]
            answer = self._answer(workspace)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "one_direct_test_assertion_and_source_function_required",
                diagnostic["errors"],
            )
        finally:
            temporary.cleanup()

    def test_missing_structured_observation_cannot_pass(self) -> None:
        temporary, _root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace).replace(
                "Diagnosis class: VERIFIED_ASSERTION_MISMATCH\n"
                "Observed result: actual=4; expected=3\n",
                "Diagnosis class: VERIFIED_ASSERTION_MISMATCH\n",
            )
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "answer_missing_structured_observed_result", diagnostic["errors"]
            )
        finally:
            temporary.cleanup()

    def test_stale_file_version_cannot_support_diagnostic(self) -> None:
        temporary, root, workspace, evidence = self._workspace()
        try:
            answer = self._answer(workspace)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            (root / "src" / "totals.py").write_text(
                "def total(values):\n    return sum(values) + 2\n",
                encoding="utf-8",
            )
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            self.assertFalse(citations["valid"])
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn("diagnostic_citations_not_valid", diagnostic["errors"])
        finally:
            temporary.cleanup()

    def test_unsupported_source_semantics_fail_closed(self) -> None:
        temporary, root, workspace, evidence = self._workspace()
        try:
            (root / "src" / "totals.py").write_text(
                "def total(values):\n    if values:\n        return sum(values) + 1\n    return 0\n",
                encoding="utf-8",
            )
            evidence = [
                _read_evidence(workspace, path)
                for path in (
                    "src/totals.py",
                    "tests/test_totals.py",
                    "failure.txt",
                )
            ]
            answer = self._answer(workspace)
            citations = validate_answer_citations(answer, evidence, workspace=workspace)
            diagnostic = evaluate_pytest_diagnostic_evidence(
                QUESTION, answer, evidence, citations
            )
            self.assertEqual(diagnostic["status"], "UNVERIFIABLE")
            self.assertIn(
                "source_function_not_statically_verifiable", diagnostic["errors"]
            )
        finally:
            temporary.cleanup()

    def test_non_diagnostic_questions_are_not_applicable(self) -> None:
        checked = evaluate_pytest_diagnostic_evidence(
            "Explain where total is defined.",
            "See src/totals.py:1.",
            (),
            {"valid": False, "citations": [], "errors": []},
        )
        self.assertEqual(checked["status"], "NOT_APPLICABLE")


if __name__ == "__main__":
    unittest.main()
