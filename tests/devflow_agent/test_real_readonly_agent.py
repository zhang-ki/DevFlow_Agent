from __future__ import annotations

import json
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from devflow_agent.real_readonly_agent import (
    ConservativeLocalTokenizer,
    LocalModelProfile,
    MAX_OUTPUT_TOKENS,
    ReadonlyAgentError,
    _parse_local_endpoint,
    bind_single_attempt_model_call,
    collect_tool_trace,
    load_local_model_profile,
    run_local_readonly_question,
    validate_answer_citations,
)
from devflow_agent.repository import AuthorizedWorkspace


class LocalModelBoundaryTests(unittest.TestCase):
    def test_live_run_requires_an_explicit_config_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-model-config-") as temporary:
            with self.assertRaises(ReadonlyAgentError) as raised:
                load_local_model_profile(Path(temporary))
        self.assertEqual(raised.exception.code, "local_model_config_missing")

    def test_remote_endpoint_is_rejected_without_echoing_config_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-model-config-") as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text(
                '[llm]\nmodel="private-model-name"\n'
                'base_url="https://api.example.invalid/v1"\n'
                'api_key="private-test-secret"\nmax_tokens=4096\n',
                encoding="utf-8",
            )
            with self.assertRaises(ReadonlyAgentError) as raised:
                load_local_model_profile(root)
        self.assertEqual(raised.exception.code, "external_model_endpoint_blocked")
        self.assertNotIn("private-test-secret", str(raised.exception))
        self.assertNotIn("private-model-name", str(raised.exception))

    def test_loopback_endpoint_is_sanitized_and_url_credentials_rejected(self) -> None:
        with patch("devflow_agent.real_readonly_agent.socket.getaddrinfo") as getaddrinfo:
            getaddrinfo.return_value = [(2, 1, 6, "", ("127.0.0.1", 11434))]
            host, port, safe_url = _parse_local_endpoint("http://127.0.0.1:11434/v1")
        self.assertEqual((host, port), ("127.0.0.1", 11434))
        self.assertEqual(safe_url, "http://127.0.0.1:11434/v1")
        with self.assertRaises(ReadonlyAgentError) as raised:
            _parse_local_endpoint("http://user:secret@127.0.0.1:11434/v1")
        self.assertNotIn("secret", str(raised.exception))

    def test_explicit_loopback_config_is_loaded_without_exposing_api_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-model-config-") as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "config.toml").write_text(
                '[llm]\nmodel="local-qwen"\n'
                'base_url="http://127.0.0.1:11434/v1"\n'
                'api_key="local-private-value"\nmax_tokens=4096\napi_type="ollama"\n',
                encoding="utf-8",
            )
            profile = load_local_model_profile(root)
        self.assertEqual(profile.model, "local-qwen")
        self.assertEqual(profile.endpoint_sanitized, "http://127.0.0.1:11434/v1")
        self.assertEqual(profile.max_tokens, MAX_OUTPUT_TOKENS)
        self.assertNotIn("local-private-value", repr(profile))

    def test_explicit_output_budget_is_bounded_without_changing_profile_default(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory(prefix="devflow-output-budget-") as temporary:
            workspace = AuthorizedWorkspace(Path(temporary))
            with self.assertRaises(ReadonlyAgentError) as raised:
                asyncio.run(
                    run_local_readonly_question(
                        workspace,
                        "Explain a function",
                        max_output_tokens=MAX_OUTPUT_TOKENS + 1,
                    )
                )
        self.assertEqual(raised.exception.code, "invalid_output_budget")

    def test_offline_token_estimator_is_deterministic_and_bounded(self) -> None:
        tokenizer = ConservativeLocalTokenizer()
        self.assertEqual(tokenizer.encode(""), [])
        self.assertEqual(len(tokenizer.encode("abcd")), 2)
        self.assertGreaterEqual(len(tokenizer.encode("你好")), 2)

    def test_single_attempt_adapter_caps_timeout_and_does_not_call_retry_wrapper(self) -> None:
        class FakeLLM:
            def __init__(self):
                self.calls = 0
                self.seen_timeout = None

            async def original(self, **kwargs):
                self.calls += 1
                self.seen_timeout = kwargs["timeout"]
                return "response"

            async def ask_tool(self, **_kwargs):
                raise AssertionError("decorated retry wrapper should be bypassed")

        FakeLLM.ask_tool.__wrapped__ = FakeLLM.original
        llm = FakeLLM()
        bind_single_attempt_model_call(llm, 45)

        import asyncio

        result = asyncio.run(llm.ask_tool(timeout=300))
        self.assertEqual(result, "response")
        self.assertEqual(llm.calls, 1)
        self.assertEqual(llm.seen_timeout, 45)

    def test_local_mode_disables_file_logging(self) -> None:
        from loguru import logger

        calls = []
        module = importlib.import_module("app.logger")
        with patch.dict(
            os.environ,
            {"DEVFLOW_AGENT_DISABLE_FILE_LOG": "1", "DEVFLOW_AGENT_LOG_LEVEL": "CRITICAL"},
        ), patch.object(module._logger, "remove"), patch.object(
            logger, "add", side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or 0
        ):
            module.define_log_level()
        self.assertTrue(calls)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0][0], sys.stderr)
        self.assertEqual(calls[0][1]["level"], "CRITICAL")

    def test_failed_sdk_initialization_closes_http_client(self) -> None:
        import asyncio

        http_client = SimpleNamespace(aclose=AsyncMock())
        profile = LocalModelProfile(
            model="local-qwen",
            base_url="http://127.0.0.1:11434/v1",
            api_type="openai",
            api_key="",
            endpoint_sanitized="http://127.0.0.1:11434/v1",
            max_tokens=64,
        )
        with tempfile.TemporaryDirectory(prefix="devflow-client-init-") as temporary:
            workspace = AuthorizedWorkspace(Path(temporary))
            with (
                patch("devflow_agent.real_readonly_agent.load_local_model_profile", return_value=profile),
                patch("devflow_agent.real_readonly_agent.probe_loopback_endpoint"),
                patch("httpx.AsyncClient", return_value=http_client),
                patch("openai.AsyncOpenAI", side_effect=RuntimeError("synthetic init failure")),
            ):
                with self.assertRaises(ReadonlyAgentError) as raised:
                    asyncio.run(run_local_readonly_question(workspace, "Explain a function"))

        self.assertEqual(raised.exception.code, "local_client_init_failed")
        http_client.aclose.assert_awaited_once()

    def test_run_timeout_cancels_agent_and_closes_local_client(self) -> None:
        import asyncio

        class StalledAgent:
            messages = []
            terminal_reason = "step_limit"
            model_calls_seen = 0

            async def run(self, _question):
                await asyncio.Event().wait()

        client = SimpleNamespace(close=AsyncMock())
        profile = LocalModelProfile(
            model="local-qwen",
            base_url="http://127.0.0.1:11434/v1",
            api_type="openai",
            api_key="",
            endpoint_sanitized="http://127.0.0.1:11434/v1",
            max_tokens=64,
        )
        with tempfile.TemporaryDirectory(prefix="devflow-run-timeout-") as temporary:
            workspace = AuthorizedWorkspace(Path(temporary))
            with (
                patch("devflow_agent.real_readonly_agent.load_local_model_profile", return_value=profile),
                patch("devflow_agent.real_readonly_agent.probe_loopback_endpoint"),
                patch("devflow_agent.real_readonly_agent.bind_single_attempt_model_call"),
                patch("devflow_agent.openmanus_agent.create_readonly_repository_agent", return_value=StalledAgent()),
                patch("httpx.AsyncClient", return_value=SimpleNamespace()),
                patch("openai.AsyncOpenAI", return_value=client),
            ):
                result = asyncio.run(
                    run_local_readonly_question(workspace, "Explain a function", timeout_seconds=1)
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "run_timeout")
        client.close.assert_awaited_once()


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="devflow-evidence-")
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "totals.py").write_text(
            "def total(values):\n    return sum(values)\n", encoding="utf-8"
        )
        evidence = AuthorizedWorkspace(self.root).read_text(
            "src/totals.py", start_line=1, end_line=2
        )
        self.read_result = {
            "result_kind": "trusted_file_read",
            "evidence_id": evidence.evidence_id,
            "relative_path": evidence.relative_path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "full_file_sha256": evidence.full_file_sha256,
            "fragment_sha256": evidence.fragment_sha256,
            "line_count": evidence.line_count,
            "content": evidence.content,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_only_read_evidence_supports_a_path_and_line_citation(self) -> None:
        checked = validate_answer_citations(
            "The total function returns sum(values) (src/totals.py:1-2).",
            [self.read_result],
        )
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["citations"][0]["relative_path"], "src/totals.py")

    def test_human_readable_line_range_citation_is_verified_against_read_evidence(self) -> None:
        source = self.root / "src" / "billing.py"
        source.write_text(
            "header = True\n\ndef total_amount(values):\n    return sum(values)\n",
            encoding="utf-8",
        )
        evidence = AuthorizedWorkspace(self.root).read_text(
            "src/billing.py", start_line=3, end_line=4
        )
        read = {
            "result_kind": "trusted_file_read",
            "evidence_id": evidence.evidence_id,
            "relative_path": evidence.relative_path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "full_file_sha256": evidence.full_file_sha256,
            "fragment_sha256": evidence.fragment_sha256,
            "line_count": evidence.line_count,
            "content": evidence.content,
        }
        checked = validate_answer_citations(
            "The helper is defined at src/billing.py lines 3-4.", [read]
        )
        self.assertTrue(checked["valid"])
        self.assertEqual(checked["citations"][0]["relative_path"], "src/billing.py")
        self.assertEqual(checked["citations"][0]["start_line"], 3)
        self.assertEqual(checked["citations"][0]["end_line"], 4)
        self.assertEqual(
            checked["citations"][0]["full_file_sha256"], evidence.full_file_sha256
        )
    def test_search_candidates_cannot_validate_citations(self) -> None:
        candidate = {"result_kind": "index_candidates_not_read_evidence", "relative_path": "src/totals.py"}
        checked = validate_answer_citations("It is in src/totals.py:1-2.", [candidate])
        self.assertFalse(checked["valid"])
        self.assertIn("no_trusted_read_evidence", checked["errors"])

    def test_unread_path_out_of_range_and_missing_citations_fail(self) -> None:
        unread = validate_answer_citations("See src/other.py:1.", [self.read_result])
        self.assertFalse(unread["valid"])
        self.assertIn("citation_not_read", unread["errors"])
        outside = validate_answer_citations("See src/totals.py:3.", [self.read_result])
        self.assertFalse(outside["valid"])
        self.assertIn("citation_outside_read_range", outside["errors"])
        missing = validate_answer_citations("The function returns the sum.", [self.read_result])
        self.assertFalse(missing["valid"])
        self.assertIn("missing_path_line_citation", missing["errors"])

    def test_tool_call_ids_are_paired_and_only_reads_become_evidence(self) -> None:
        arguments = json.dumps({"path": "src/totals.py", "start_line": 1, "end_line": 2})
        messages = [
            SimpleNamespace(
                role="assistant",
                tool_calls=[SimpleNamespace(id="read-1", function=SimpleNamespace(name="repository_readonly_read", arguments=arguments))],
            ),
            SimpleNamespace(
                role="tool",
                tool_call_id="read-1",
                name="repository_readonly_read",
                content="Observed output of cmd `repository_readonly_read` executed:\n" + json.dumps(self.read_result),
            ),
        ]
        trace, reads, issues = collect_tool_trace(messages)
        self.assertEqual(issues, [])
        self.assertEqual(trace[0]["tool_call_id"], "read-1")
        self.assertEqual(trace[0]["status"], "completed")
        self.assertEqual(len(reads), 1)

        recoverable_error = {
            "error": "invalid_line_range",
            "recoverable": True,
            "relative_path": "src/totals.py",
            "details": {"line_count": 2, "max_lines": 500},
        }
        recovered_messages = [
            messages[0],
            SimpleNamespace(
                role="tool",
                tool_call_id="read-1",
                name="repository_readonly_read",
                content="Observed output of cmd repository_readonly_read executed:\n"
                + json.dumps(recoverable_error),
            ),
            SimpleNamespace(
                role="assistant",
                tool_calls=[SimpleNamespace(id="read-2", function=SimpleNamespace(
                    name="repository_readonly_read",
                    arguments=json.dumps({"path": "src/totals.py"}),
                ))],
            ),
            SimpleNamespace(
                role="tool",
                tool_call_id="read-2",
                name="repository_readonly_read",
                content="Observed output of cmd repository_readonly_read executed:\n"
                + json.dumps(self.read_result),
            ),
        ]
        recovered_trace, recovered_reads, recovered_issues = collect_tool_trace(recovered_messages)
        self.assertEqual(recovered_issues, [])
        self.assertEqual(recovered_trace[0]["status"], "recovered")
        self.assertEqual(recovered_trace[0]["recovered_by_tool_call_id"], "read-2")
        self.assertEqual(len(recovered_reads), 1)

        tampered_result = dict(self.read_result, fragment_sha256="0" * 64)
        tampered_messages = [
            messages[0],
            SimpleNamespace(
                role="tool",
                tool_call_id="read-1",
                name="repository_readonly_read",
                content="Observed output of cmd repository_readonly_read executed:\n"
                + json.dumps(tampered_result),
            ),
        ]
        _, tampered_reads, tampered_issues = collect_tool_trace(tampered_messages)
        self.assertEqual(tampered_reads, [])
        self.assertIn("incomplete_read_evidence", tampered_issues)

        orphan = [SimpleNamespace(role="tool", tool_call_id="missing", name="x", content="{}")]
        _, _, orphan_issues = collect_tool_trace(orphan)
        self.assertIn("tool_result_without_matching_call", orphan_issues)


if __name__ == "__main__":
    unittest.main()
