from __future__ import annotations

import unittest

from devflow_agent.answer_quality import (
    evaluate_answer_quality,
    evaluate_arithmetic_claims,
    extract_expected_numeric_claims,
)
from devflow_agent.diagnostics import DiagnosticReport


def _citation(valid: bool = True) -> dict:
    return {
        "valid": valid,
        "citations": [
            {"relative_path": "src/totals.py", "start_line": 1, "end_line": 2}
        ]
        if valid
        else [],
        "errors": [] if valid else ["invalid_citation_format"],
    }


class M2OutputContractTests(unittest.TestCase):
    def _numeric(self, answer: str) -> dict:
        expected = extract_expected_numeric_claims("Explain sum([-100, 50, -200])")
        return evaluate_arithmetic_claims(
            answer, required=True, expected_claims=expected
        )

    def test_normal_compact_answer_passes(self) -> None:
        answer = (
            "Summary: total is constant.\n"
            "Root cause: implementation ignores values.\n"
            "Evidence: src/totals.py:1-2 sha256 abc.\n"
            "Confidence: high\nUnconfirmed: causality not rerun.\n"
            "Expression sum([-100, 50, -200]) = -250."
        )
        numeric = self._numeric(answer)
        result = evaluate_answer_quality(
            answer=answer,
            run_status="completed",
            terminal_reason="final_answer",
            trace_issues=[],
            read_evidence_count=1,
            citation_check=_citation(),
            numeric_check=numeric,
        )
        self.assertEqual(result["status"], "PASS")

    def test_truncated_answer_fails_closed(self) -> None:
        numeric = self._numeric("Expression sum([-100, 50, -200]) = -250")
        result = evaluate_answer_quality(
            answer="Summary: partial",
            run_status="completed",
            terminal_reason="model_truncated",
            trace_issues=[],
            read_evidence_count=1,
            citation_check=_citation(),
            numeric_check=numeric,
        )
        self.assertEqual(result["status"], "MODEL_OR_TOOL_FAILURE")

    def test_invalid_citation_fails_closed(self) -> None:
        numeric = self._numeric("Expression sum([-100, 50, -200]) = -250")
        result = evaluate_answer_quality(
            answer="src/totals.py:1-2; Expression sum([-100, 50, -200]) = -250",
            run_status="completed",
            terminal_reason="final_answer",
            trace_issues=[],
            read_evidence_count=1,
            citation_check=_citation(False),
            numeric_check=numeric,
        )
        self.assertEqual(result["status"], "INVALID_CITATION")

    def test_missing_evidence_is_not_success(self) -> None:
        numeric = self._numeric("Expression sum([-100, 50, -200]) = -250")
        result = evaluate_answer_quality(
            answer="Summary only",
            run_status="completed",
            terminal_reason="final_answer",
            trace_issues=[],
            read_evidence_count=0,
            citation_check={
                "valid": False,
                "citations": [],
                "errors": ["no_trusted_read_evidence"],
            },
            numeric_check=numeric,
        )
        self.assertEqual(result["status"], "MISSING_EVIDENCE")

    def test_compact_report_caps_evidence_and_omits_full_payloads(self) -> None:
        evidence = [
            {
                "relative_path": f"src/f{i}.py",
                "start_line": i,
                "end_line": i + 1,
                "full_file_sha256": "a" * 64,
                "summary": "source" * 100,
            }
            for i in range(1, 8)
        ]
        report = DiagnosticReport(
            schema_version=1,
            status="PASS",
            exit_code=1,
            parsed_failures=[
                {
                    "test_nodeid": "tests/test_x.py::test_x",
                    "message": "assertion failed",
                    "root_cause": {
                        "classification": "assertion_mismatch",
                        "confidence": "medium",
                    },
                    "test_location": {"relative_path": "tests/test_x.py", "line": 2},
                    "source_location": {"relative_path": "src/f1.py", "line": 1},
                    "evidence": evidence,
                    "uncertainties": ["u1", "u2", "u3", "u4"],
                    "log_excerpt": "secretly long source payload",
                }
            ],
            metrics={},
        )
        compact = report.to_compact_dict()
        item = compact["failures"][0]
        self.assertEqual(len(item["evidence"]), 3)
        self.assertLessEqual(len(item["unconfirmed"]), 3)
        self.assertNotIn("log_excerpt", item)
        self.assertNotIn("search_candidates", item)


if __name__ == "__main__":
    unittest.main()
