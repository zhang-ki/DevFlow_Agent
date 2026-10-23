from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.schema import AgentState

from devflow_agent.answer_quality import (
    build_numeric_correction_prompt,
    evaluate_answer_quality,
    evaluate_arithmetic_claims,
    evaluate_numeric_expression,
)
from devflow_agent.real_readonly_agent import (
    LocalModelProfile,
    _run_bounded_agent_correction,
    run_local_readonly_question,
    validate_answer_citations,
)
from devflow_agent.repository import AuthorizedWorkspace


class NumericValidationTests(unittest.TestCase):
    def test_supported_arithmetic_and_python_precedence(self) -> None:
        self.assertEqual(evaluate_numeric_expression("sum([-100, 50, -200])"), -250)
        self.assertEqual(evaluate_numeric_expression("-100 + 50 - 200"), -250)
        self.assertEqual(evaluate_numeric_expression("2 * (3 + 4)"), 14)
        self.assertEqual(evaluate_numeric_expression("0"), 0)
        self.assertEqual(evaluate_numeric_expression("sum([])"), 0)
        self.assertAlmostEqual(evaluate_numeric_expression("0.1 + 0.2"), 0.3)

    def test_claims_detect_mismatch_tolerance_and_unverifiable_cases(self) -> None:
        wrong = evaluate_arithmetic_claims(
            "sum([-100, 50, -200]) returns -350"
        )
        self.assertEqual(wrong["status"], "NUMERIC_MISMATCH")
        self.assertEqual(wrong["claims"][0]["actual_value"], -250)
        self.assertEqual(
            evaluate_arithmetic_claims("0.1 + 0.2 = 0.3000000001")["status"],
            "PASS",
        )
        for answer in (
            "1 / 0 = 0",
            "sum(values) returns 5",
            "sum([1 + 2]) returns 3",
            "sum((1, 2)) returns 3",
            "__import__('os').system('whoami') = 0",
        ):
            with self.subTest(answer=answer):
                checked = evaluate_arithmetic_claims(answer)
                self.assertEqual(checked["status"], "UNVERIFIABLE")

    def test_structured_expression_and_result_sections_are_verified(self) -> None:
        answer = (
            "**Expression:** `sum([-100, 50, -200])`\n"
            "**Calculation Steps:** -100, then -50, then -250\n"
            "**Result:** `-250`"
        )
        expected = [("sum([-100, 50, -200])", -250)]
        checked = evaluate_arithmetic_claims(
            answer, required=True, expected_claims=expected
        )
        self.assertEqual(checked["status"], "PASS")
        self.assertEqual(checked["claims_checked"], 1)
        self.assertEqual(checked["claims"][0]["actual_value"], -250)

    def test_required_numeric_claim_with_zero_extractions_is_unverifiable(self) -> None:
        checked = evaluate_arithmetic_claims(
            "The total is shown in the table.",
            required=True,
            expected_claims=[("sum([-100, 50, -200])", -250)],
        )
        self.assertEqual(checked["status"], "UNVERIFIABLE")
        self.assertEqual(checked["claims_checked"], 0)
        self.assertIn("expected_numeric_claim_missing", checked["errors"])

    def test_non_numeric_answer_is_not_applicable(self) -> None:
        checked = evaluate_arithmetic_claims("The helper is in src/totals.py:1-2.")
        self.assertEqual(checked["status"], "NOT_APPLICABLE")
        self.assertEqual(checked["quality_state"], "NOT_APPLICABLE")

    def test_explicit_unsupported_numeric_claim_is_not_ignored(self) -> None:
        checked = evaluate_arithmetic_claims("sum(values) returns 6")
        self.assertEqual(checked["claims_checked"], 1)
        self.assertEqual(checked["status"], "UNVERIFIABLE")

    def test_correction_prompt_is_limited_to_verified_mismatches(self) -> None:
        result = evaluate_arithmetic_claims("sum([-100, 50, -200]) is -350")
        prompt = build_numeric_correction_prompt(result)
        self.assertIn("deterministic calculation is -250", prompt)
        with self.assertRaises(ValueError):
            build_numeric_correction_prompt(evaluate_arithmetic_claims("2 + 2 = 4"))


class QualityStateTests(unittest.TestCase):
    def _evaluate(
        self,
        *,
        answer: str = "See src/totals.py:1-2.",
        run_status: str = "completed",
        terminal_reason: str = "final_answer",
        trace_issues: tuple[str, ...] = (),
        read_count: int = 1,
        citation_valid: bool = True,
        citation_errors: tuple[str, ...] = (),
        numeric_answer: str | None = None,
    ) -> dict:
        numeric = evaluate_arithmetic_claims(answer if numeric_answer is None else numeric_answer)
        citation = {
            "valid": citation_valid,
            "citations": [{"relative_path": "src/totals.py", "start_line": 1, "end_line": 2}]
            if citation_valid or citation_errors
            else [],
            "errors": list(citation_errors),
        }
        return evaluate_answer_quality(
            answer=answer,
            run_status=run_status,
            terminal_reason=terminal_reason,
            trace_issues=trace_issues,
            read_evidence_count=read_count,
            citation_check=citation,
            numeric_check=numeric,
        )

    def test_statuses_separate_execution_evidence_citation_and_numeric_quality(self) -> None:
        self.assertEqual(self._evaluate()["status"], "PASS")
        self.assertEqual(
            self._evaluate(numeric_answer="sum([-100,50,-200]) returns -350")["status"],
            "NUMERIC_MISMATCH",
        )
        self.assertEqual(
            self._evaluate(citation_valid=False, citation_errors=("citation_outside_read_range",))["status"],
            "INVALID_CITATION",
        )
        self.assertEqual(self._evaluate(read_count=0)["status"], "MISSING_EVIDENCE")
        self.assertEqual(
            self._evaluate(run_status="failed", terminal_reason="provider_error")["status"],
            "MODEL_OR_TOOL_FAILURE",
        )
        self.assertEqual(
            self._evaluate(numeric_answer="sum(values) returns 5")["status"],
            "UNVERIFIABLE",
        )

    def test_pass_scope_does_not_claim_natural_language_entailment(self) -> None:
        result = self._evaluate()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["components"]["semantic_entailment"], "NOT_AUTOMATICALLY_VERIFIED")
        self.assertIn("does not prove", result["scope_note"])


class CitationFreshnessTests(unittest.TestCase):
    def _evidence(self, workspace: AuthorizedWorkspace) -> dict:
        snapshot = workspace.read_text("src/totals.py")
        end_line = min(2, snapshot.line_count)
        result = workspace.read_text("src/totals.py", start_line=1, end_line=end_line)
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

    def test_valid_citation_is_tied_to_current_file_hash_and_read_range(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-m15-citation-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "totals.py").write_text("def total(x):\n    return sum(x)\n", encoding="utf-8")
            workspace = AuthorizedWorkspace(root)
            evidence = self._evidence(workspace)
            checked = validate_answer_citations(
                "The helper is at src/totals.py:1-2.",
                [evidence],
                workspace=workspace,
            )
            self.assertTrue(checked["valid"])
            self.assertEqual(
                checked["citations"][0]["full_file_sha256"],
                evidence["full_file_sha256"],
            )

    def test_markdown_and_plain_citation_forms_are_equivalent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-m15-citation-forms-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "totals.py").write_text(
                "def total(x):\n    return sum(x)\n", encoding="utf-8"
            )
            workspace = AuthorizedWorkspace(root)
            evidence = self._evidence(workspace)
            forms = (
                "src/totals.py lines 1-2",
                "src/totals.py:1-2",
                "`src/totals.py` lines 1-2",
                "`src/totals.py:1-2`",
            )
            for form in forms:
                with self.subTest(form=form):
                    checked = validate_answer_citations(
                        f"The helper is at {form}.",
                        [evidence],
                        workspace=workspace,
                    )
                    self.assertTrue(checked["valid"], checked)
                    self.assertEqual(checked["citations"][0]["relative_path"], "src/totals.py")

    def test_modified_file_invalidates_previously_read_citation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-m15-stale-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            path = root / "src" / "totals.py"
            path.write_text("def total(x):\n    return sum(x)\n", encoding="utf-8")
            workspace = AuthorizedWorkspace(root)
            evidence = self._evidence(workspace)
            path.write_text("def total(x):\n    return 0\n", encoding="utf-8")
            checked = validate_answer_citations(
                "The helper is at src/totals.py:1-2.",
                [evidence],
                workspace=workspace,
            )
            self.assertFalse(checked["valid"])
            self.assertIn("citation_stale_file_version", checked["errors"])

    def test_malformed_citation_range_is_explicitly_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-m15-bad-cite-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "totals.py").write_text("x = 1\n", encoding="utf-8")
            workspace = AuthorizedWorkspace(root)
            evidence = self._evidence(workspace)
            checked = validate_answer_citations(
                "See src/totals.py:0.", [evidence], workspace=workspace
            )
            self.assertFalse(checked["valid"])
            self.assertIn("invalid_citation_format", checked["errors"])


class BoundedCorrectionTests(unittest.TestCase):
    def test_correction_uses_existing_agent_steps_and_obeys_budget(self) -> None:
        class FakeAgent:
            def __init__(self) -> None:
                self.state = AgentState.IDLE
                self.current_step = 5
                self.max_steps = 8
                self.terminal_reason = "final_answer"
                self.messages: list[tuple[str, str]] = []
                self.step_calls = 0

            def update_memory(self, role: str, content: str) -> None:
                self.messages.append((role, content))

            @asynccontextmanager
            async def state_context(self, new_state: AgentState):
                previous = self.state
                self.state = new_state
                try:
                    yield
                finally:
                    self.state = previous

            async def step(self) -> None:
                self.step_calls += 1
                if self.step_calls == 2:
                    self.terminal_reason = "final_answer"
                    self.state = AgentState.FINISHED

        import asyncio

        agent = FakeAgent()
        steps = asyncio.run(
            _run_bounded_agent_correction(
                agent,
                "recompute the verified mismatch",
                step_budget=2,
            )
        )
        self.assertEqual(steps, 2)
        self.assertEqual(agent.step_calls, 2)
        self.assertEqual(agent.current_step, 2)
        self.assertEqual(agent.state, AgentState.IDLE)
        self.assertEqual(agent.messages[-1], ("user", "recompute the verified mismatch"))
        over_budget_agent = FakeAgent()
        over_budget = asyncio.run(
            _run_bounded_agent_correction(
                over_budget_agent, "reject excessive budget", step_budget=13
            )
        )
        self.assertEqual(over_budget, 0)
        self.assertEqual(over_budget_agent.step_calls, 0)


class FixedEvaluationSetTests(unittest.TestCase):
    def test_all_fixed_cases_match_expected_status(self) -> None:
        root = Path(__file__).resolve().parents[2]
        cases_path = root / "evals" / "m1_5_answer_quality.jsonl"
        cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(cases), 10)
        identifiers = [case["id"] for case in cases]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for case in cases:
            with self.subTest(case=case["id"]):
                result = evaluate_answer_quality(
                    answer=case["answer"],
                    run_status=case["run_status"],
                    terminal_reason=case["terminal_reason"],
                    trace_issues=case["trace_issues"],
                    read_evidence_count=case["read_evidence_count"],
                    citation_check=case["citation_check"],
                    numeric_check=evaluate_arithmetic_claims(case["answer"]),
                )
                self.assertEqual(result["status"], case["expected_status"])


class IntegratedCorrectionTests(unittest.TestCase):
    def _run_synthetic_agent(self, corrected_answer: str) -> dict:
        class FakeAgent:
            def __init__(self, messages: list[object]) -> None:
                self.messages = messages
                self.state = AgentState.IDLE
                self.current_step = 4
                self.max_steps = 6
                self.terminal_reason = "final_answer"
                self.model_calls_seen = 3
                self.tool_error_recoveries_used = 0
                self.corrected_answer = corrected_answer

            async def run(self, _question: str) -> str:
                return "synthetic initial response"

            def update_memory(self, role: str, content: str) -> None:
                self.messages.append(SimpleNamespace(role=role, content=content, tool_calls=[]))

            @asynccontextmanager
            async def state_context(self, new_state: AgentState):
                previous = self.state
                self.state = new_state
                try:
                    yield
                finally:
                    self.state = previous

            async def step(self) -> str:
                self.model_calls_seen += 1
                self.messages.append(
                    SimpleNamespace(role="assistant", content=self.corrected_answer, tool_calls=[])
                )
                self.terminal_reason = "final_answer"
                self.state = AgentState.FINISHED
                return self.corrected_answer

        with tempfile.TemporaryDirectory(prefix="devflow-m15-correction-") as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "totals.py").write_text(
                "def total(values):\n    return sum(values)\n", encoding="utf-8"
            )
            workspace = AuthorizedWorkspace(root)
            evidence = workspace.read_text("src/totals.py", start_line=1, end_line=2)
            tool_result = {
                "result_kind": "trusted_file_read",
                "evidence_id": evidence.evidence_id,
                "workspace_id": evidence.workspace_id,
                "relative_path": evidence.relative_path,
                "start_line": evidence.start_line,
                "end_line": evidence.end_line,
                "line_count": evidence.line_count,
                "content": evidence.content,
                "full_file_sha256": evidence.full_file_sha256,
                "fragment_sha256": evidence.fragment_sha256,
                "file_size_bytes": evidence.file_size_bytes,
                "task_id": None,
                "run_id": None,
            }
            call_id = "trusted-read-1"
            messages = [
                SimpleNamespace(
                    role="assistant",
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(
                                name="repository_readonly_read",
                                arguments=json.dumps({"path": "src/totals.py", "start_line": 1, "end_line": 2}),
                            ),
                        )
                    ],
                ),
                SimpleNamespace(
                    role="tool",
                    tool_call_id=call_id,
                    name="repository_readonly_read",
                    content="Observed output of cmd repository_readonly_read executed:\n"
                    + json.dumps(tool_result),
                ),
                SimpleNamespace(
                    role="assistant",
                    content="sum([-100, 50, -200]) returns -350. See src/totals.py:1-2.",
                    tool_calls=[],
                ),
            ]
            agent = FakeAgent(messages)
            profile = LocalModelProfile(
                model="synthetic-local-qwen",
                base_url="http://127.0.0.1:11434/v1",
                api_type="openai",
                api_key="",
                endpoint_sanitized="http://127.0.0.1:11434/v1",
                max_tokens=64,
            )
            client = SimpleNamespace(close=AsyncMock())
            http_client = SimpleNamespace(aclose=AsyncMock())
            with (
                patch("devflow_agent.real_readonly_agent.load_local_model_profile", return_value=profile),
                patch("devflow_agent.real_readonly_agent.probe_loopback_endpoint"),
                patch("devflow_agent.real_readonly_agent.bind_single_attempt_model_call"),
                patch("devflow_agent.openmanus_agent.create_readonly_repository_agent", return_value=agent),
                patch("httpx.AsyncClient", return_value=http_client),
                patch("openai.AsyncOpenAI", return_value=client),
            ):
                return asyncio.run(
                    run_local_readonly_question(
                        workspace,
                        "Check the signed total and cite the helper.",
                        max_steps=6,
                        timeout_seconds=10,
                    )
                )

    def test_detected_mismatch_gets_at_most_one_bounded_correction(self) -> None:
        corrected = self._run_synthetic_agent(
            "The signed total is -250 because sum([-100, 50, -200]) returns -250. See src/totals.py:1-2."
        )
        self.assertEqual(corrected["quality_correction"]["attempts"], 1)
        self.assertEqual(corrected["quality_correction"]["initial_quality_status"], "NUMERIC_MISMATCH")
        self.assertEqual(corrected["quality_correction"]["final_quality_status"], "PASS")
        self.assertEqual(corrected["status"], "verified")
        self.assertEqual(corrected["model_calls"], 4)
        self.assertEqual(corrected["quality_correction"]["initial_agent_steps"], 4)
        self.assertEqual(corrected["quality_correction"]["initial_model_calls"], 3)
        self.assertEqual(corrected["quality_correction"]["available_steps"], 2)

        still_wrong = self._run_synthetic_agent(
            "sum([-100, 50, -200]) returns -350. See src/totals.py:1-2."
        )
        self.assertEqual(still_wrong["quality_correction"]["attempts"], 1)
        self.assertEqual(still_wrong["answer_quality"]["status"], "NUMERIC_MISMATCH")
        self.assertEqual(still_wrong["status"], "failed")


if __name__ == "__main__":
    unittest.main()
