from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import httpx

from devflow_agent.openmanus_agent import create_readonly_repository_agent
from devflow_agent.real_readonly_agent import (
    collect_tool_trace,
    validate_answer_citations,
)
from devflow_agent.repository import AuthorizedWorkspace, RepoError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "repos" / "readonly_demo"


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


@contextmanager
def load_real_agent_sources() -> Iterator[dict[str, type]]:
    """Import the fixed Agent and ToolCollection through normal package imports.

    The test uses the project's example config only; it refuses to import if a
    local config.toml or mcp.json exists. Process/network APIs are guarded during
    import, and the Loguru file sink is disabled so no project log is created.
    """
    if (REPOSITORY_ROOT / "config" / "config.toml").exists():
        raise AssertionError("refusing to load a project-local config.toml")
    if (REPOSITORY_ROOT / "config" / "mcp.json").exists():
        raise AssertionError("refusing to load project-local MCP configuration")

    prefix_names = {
        name for name in sys.modules if name == "app" or name.startswith("app.")
    }
    previous = {name: sys.modules[name] for name in prefix_names}
    for name in prefix_names:
        del sys.modules[name]

    try:
        from loguru import logger as loguru_logger

        with ExitStack() as guards:
            guards.enter_context(
                patch.object(
                    subprocess, "Popen", side_effect=AssertionError("subprocess import")
                )
            )
            guards.enter_context(
                patch.object(
                    subprocess, "run", side_effect=AssertionError("subprocess import")
                )
            )
            guards.enter_context(
                patch.object(
                    socket, "socket", side_effect=AssertionError("socket import")
                )
            )
            guards.enter_context(
                patch.object(
                    asyncio,
                    "create_subprocess_exec",
                    side_effect=AssertionError("subprocess import"),
                )
            )
            guards.enter_context(
                patch.object(
                    asyncio,
                    "create_subprocess_shell",
                    side_effect=AssertionError("subprocess import"),
                )
            )
            guards.enter_context(
                patch.object(httpx, "Client", side_effect=AssertionError("HTTP import"))
            )
            guards.enter_context(
                patch.object(
                    httpx, "AsyncClient", side_effect=AssertionError("HTTP import")
                )
            )
            with patch.object(loguru_logger, "add", return_value=0):
                agent_module = importlib.import_module("app.agent.toolcall")
                collection_module = importlib.import_module("app.tool.tool_collection")
                agent_package = importlib.import_module("app.agent")
                tool_package = importlib.import_module("app.tool")
                schema = importlib.import_module("app.schema")
                llm_module = importlib.import_module("app.llm")
                if agent_package.ToolCallAgent is not agent_module.ToolCallAgent:
                    raise AssertionError(
                        "lazy Agent export did not resolve the real class"
                    )
                if tool_package.ToolCollection is not collection_module.ToolCollection:
                    raise AssertionError(
                        "lazy Tool export did not resolve the real class"
                    )
                yield {
                    "ToolCallAgent": agent_module.ToolCallAgent,
                    "ToolCollection": collection_module.ToolCollection,
                    "Function": schema.Function,
                    "Message": schema.Message,
                    "ToolCall": schema.ToolCall,
                    "AgentState": schema.AgentState,
                    "LLM": llm_module.LLM,
                }
    finally:
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(previous)


def run_immediate(coroutine):
    """Run a fully synchronous async call chain without creating loopback sockets."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("the offline Agent Loop unexpectedly suspended")


def tool_call(
    types_: dict[str, type], call_id: str, name: str, arguments: dict
) -> object:
    return types_["ToolCall"](
        id=call_id,
        function=types_["Function"](
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
        ),
    )


def make_scripted_llm(llm_type: type, responses: list, on_call=None):
    """Build a deterministic subclass of the real OpenManus LLM class."""

    class ScriptedLLM(llm_type):
        def __new__(cls, *args, **kwargs):
            # Avoid OpenManus' cached constructor and all provider setup.
            return object.__new__(cls)

        def __init__(self, scripted_responses, callback=None) -> None:
            self.responses = list(scripted_responses)
            self.on_call = callback
            self.calls: list[dict] = []

        async def ask_tool(
            self, *, messages, system_msgs=None, tools=None, tool_choice=None
        ):
            snapshot = [message.to_dict() for message in messages]
            self.calls.append(
                {
                    "messages": snapshot,
                    "tools": tools,
                    "tool_choice": str(tool_choice),
                }
            )
            if self.on_call:
                self.on_call(len(self.calls), snapshot, tools)
            if not self.responses:
                raise AssertionError("Agent exceeded the scripted response budget")
            return self.responses.pop(0)

    return ScriptedLLM(responses, on_call)


class OpenManusAgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="devflow-m1-2-loop-", dir=REPOSITORY_ROOT / ".venv"
        )
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(FIXTURE_ROOT, self.repo)
        self.before = tree_hashes(self.repo)
        self.workspace = AuthorizedWorkspace(self.repo)

    def tearDown(self) -> None:
        self.assertEqual(tree_hashes(self.repo), self.before)
        self.temporary.cleanup()

    def make_agent(self, source_types, responses, *, on_call=None, max_steps=8):
        llm = make_scripted_llm(source_types["LLM"], responses, on_call=on_call)
        llm.on_call = on_call
        self.assertIsInstance(llm, source_types["LLM"])
        agent = create_readonly_repository_agent(
            self.workspace,
            tool_collection_factory=source_types["ToolCollection"],
            tool_call_agent_class=source_types["ToolCallAgent"],
            llm=llm,
            max_steps=max_steps,
        )
        self.assertIn(source_types["ToolCallAgent"], type(agent).__mro__)
        self.assertEqual(
            {tool.name for tool in agent.available_tools.tools},
            {"repository_search_readonly", "repository_readonly_read"},
        )
        return agent, llm

    def guard_external_effects(self):
        stack = ExitStack()
        blocked = AssertionError("external process/network use is forbidden")
        stack.enter_context(patch.object(subprocess, "Popen", side_effect=blocked))
        stack.enter_context(patch.object(subprocess, "run", side_effect=blocked))
        stack.enter_context(patch.object(socket, "socket", side_effect=blocked))
        stack.enter_context(
            patch.object(asyncio, "create_subprocess_exec", side_effect=blocked)
        )
        stack.enter_context(
            patch.object(asyncio, "create_subprocess_shell", side_effect=blocked)
        )
        stack.enter_context(patch.object(httpx, "Client", side_effect=blocked))
        stack.enter_context(patch.object(httpx, "AsyncClient", side_effect=blocked))
        return stack

    def test_real_agent_loop_searches_reads_and_answers_with_matching_evidence(
        self,
    ) -> None:
        with load_real_agent_sources() as source_types:
            root = REPOSITORY_ROOT
            self.assertEqual(
                Path(sys.modules["app.agent.toolcall"].__file__).resolve(),
                root / "app" / "agent" / "toolcall.py",
            )
            self.assertEqual(
                Path(sys.modules["app.tool.tool_collection"].__file__).resolve(),
                root / "app" / "tool" / "tool_collection.py",
            )
            self.assertEqual(
                source_types["ToolCallAgent"].__module__, "app.agent.toolcall"
            )
            self.assertEqual(
                source_types["ToolCollection"].__module__,
                "app.tool.tool_collection",
            )
            self.assertNotIn("app.agent.mcp", sys.modules)
            self.assertNotIn("app.tool.mcp", sys.modules)
            self.assertNotIn("app.tool.bash", sys.modules)
            self.assertNotIn("app.tool.python_execute", sys.modules)
            self.assertNotIn("app.tool.str_replace_editor", sys.modules)

            evidence_seen: list[dict] = []

            def inspect_turn(call_number: int, messages: list[dict], schemas) -> None:
                self.assertEqual(
                    {schema["function"]["name"] for schema in schemas},
                    {"repository_search_readonly", "repository_readonly_read"},
                )
                tool_messages = [
                    message for message in messages if message["role"] == "tool"
                ]
                if call_number == 2:
                    self.assertEqual(len(tool_messages), 1)
                    self.assertEqual(tool_messages[0]["tool_call_id"], "search-1")
                    result = json.loads(tool_messages[0]["content"].split("\n", 1)[-1])
                    self.assertEqual(
                        result["result_kind"], "index_candidates_not_read_evidence"
                    )
                    self.assertIn(
                        "src/totals.py",
                        {hit["relative_path"] for hit in result["results"]},
                    )
                    self.assertTrue(
                        all(not hit["read_evidence"] for hit in result["results"])
                    )
                if call_number == 3:
                    self.assertEqual(len(tool_messages), 2)
                    self.assertEqual(tool_messages[1]["tool_call_id"], "read-2")
                    result = json.loads(tool_messages[1]["content"].split("\n", 1)[-1])
                    self.assertEqual(result["result_kind"], "trusted_file_read")
                    self.assertEqual(result["relative_path"], "src/totals.py")
                    self.assertEqual(result["start_line"], 3)
                    self.assertEqual(result["end_line"], 5)
                    self.assertEqual(
                        result["full_file_sha256"],
                        hashlib.sha256(
                            (self.repo / "src" / "totals.py").read_bytes()
                        ).hexdigest(),
                    )
                    self.assertIn("return sum(values)", result["content"])
                    evidence_seen.append(result)

            messages = source_types["Message"]
            responses = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "search-1",
                            "repository_search_readonly",
                            {"query": "sum numeric values", "limit": 5},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-2",
                            "repository_readonly_read",
                            {"path": "src/totals.py", "start_line": 3, "end_line": 5},
                        )
                    ],
                ),
                messages.assistant_message(
                    "The function is in src/totals.py:3-5 and returns sum(values)."
                ),
            ]
            agent, llm = self.make_agent(source_types, responses, on_call=inspect_turn)
            with self.guard_external_effects():
                result = run_immediate(
                    agent.run("Where is the total function and what does it do?")
                )

            self.assertEqual(len(llm.calls), 3)
            self.assertEqual(len(evidence_seen), 1)
            self.assertIn("src/totals.py:3-5", agent.messages[-1].content)
            self.assertIn("sum(values)", agent.messages[-1].content)
            self.assertIn(agent.messages[-1].content, result)
            self.assertEqual(
                [
                    message.tool_call_id
                    for message in agent.messages
                    if message.role == "tool"
                ],
                ["search-1", "read-2"],
            )

    def test_invalid_read_range_is_returned_to_model_and_recovers_once(self) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            responses = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "search-recover",
                            "repository_search_readonly",
                            {"query": "total"},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-bad-range",
                            "repository_readonly_read",
                            {"path": "src/totals.py", "start_line": 1, "end_line": 20},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-corrected",
                            "repository_readonly_read",
                            {"path": "src/totals.py"},
                        )
                    ],
                ),
                messages.assistant_message(
                    "The total function returns sum(values) (src/totals.py:3-5)."
                ),
            ]

            def inspect_turn(
                call_number: int, conversation: list[dict], _schemas
            ) -> None:
                tool_messages = [
                    message for message in conversation if message["role"] == "tool"
                ]
                if call_number == 2:
                    search_result = json.loads(
                        tool_messages[-1]["content"].split("\n", 1)[-1]
                    )
                    total_hit = next(
                        item
                        for item in search_result["results"]
                        if item["relative_path"] == "src/totals.py"
                    )
                    self.assertEqual(total_hit["line_count"], 10)
                if call_number == 3:
                    self.assertEqual(
                        tool_messages[-1]["tool_call_id"], "read-bad-range"
                    )
                    error = json.loads(tool_messages[-1]["content"].split("\n", 1)[-1])
                    self.assertEqual(error["error"], "invalid_line_range")
                    self.assertTrue(error["recoverable"])
                    self.assertEqual(error["details"]["line_count"], 10)
                    self.assertEqual(error["details"]["max_lines"], 500)
                if call_number == 4:
                    self.assertEqual(
                        tool_messages[-1]["tool_call_id"], "read-corrected"
                    )
                    read_result = json.loads(
                        tool_messages[-1]["content"].split("\n", 1)[-1]
                    )
                    self.assertEqual(read_result["result_kind"], "trusted_file_read")
                    self.assertEqual(read_result["relative_path"], "src/totals.py")
                    self.assertEqual(
                        (read_result["start_line"], read_result["end_line"]), (1, 10)
                    )
                    self.assertEqual(read_result["line_count"], 10)
                    self.assertEqual(
                        read_result["full_file_sha256"],
                        hashlib.sha256(
                            (self.repo / "src" / "totals.py").read_bytes()
                        ).hexdigest(),
                    )

            agent, llm = self.make_agent(source_types, responses, on_call=inspect_turn)
            with self.guard_external_effects():
                run_immediate(agent.run("Explain the total function."))
            trace, reads, issues = collect_tool_trace(agent.messages)
            self.assertEqual(len(llm.calls), 4)
            self.assertEqual(agent.terminal_reason, "final_answer")
            self.assertEqual(agent.tool_error_recoveries_used, 1)
            self.assertEqual(issues, [])
            self.assertEqual(trace[1]["status"], "recovered")
            self.assertEqual(trace[1]["recovered_by_tool_call_id"], "read-corrected")
            self.assertEqual(len(reads), 1)
            checked = validate_answer_citations(agent.messages[-1].content, reads)
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["citations"][0]["relative_path"], "src/totals.py")
            self.assertEqual(checked["citations"][0]["start_line"], 3)
            self.assertEqual(
                checked["citations"][0]["full_file_sha256"],
                hashlib.sha256(
                    (self.repo / "src" / "totals.py").read_bytes()
                ).hexdigest(),
            )

    def test_recovery_budget_exhaustion_fails_with_matched_tool_results(self) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            responses = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "search-budget",
                            "repository_search_readonly",
                            {"query": "total"},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-bad-1",
                            "repository_readonly_read",
                            {"path": "src/totals.py", "start_line": 1, "end_line": 20},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-bad-2",
                            "repository_readonly_read",
                            {"path": "src/totals.py", "start_line": 1, "end_line": 30},
                        )
                    ],
                ),
                messages.assistant_message("Should not be used."),
            ]
            agent, llm = self.make_agent(source_types, responses)
            with self.guard_external_effects():
                run_immediate(agent.run("Explain the total function."))
            tool_messages = [
                message for message in agent.messages if message.role == "tool"
            ]
            self.assertEqual(len(llm.calls), 3)
            self.assertEqual(
                [message.tool_call_id for message in tool_messages],
                ["search-budget", "read-bad-1", "read-bad-2"],
            )
            self.assertEqual(agent.terminal_reason, "tool_error_recovery_limit")
            self.assertEqual(agent.tool_error_recoveries_used, 1)
            self.assertFalse(
                any(
                    message.role == "assistant"
                    and message.content == "Should not be used."
                    for message in agent.messages
                )
            )
            trace, reads, issues = collect_tool_trace(agent.messages)
            self.assertEqual(len(reads), 0)
            self.assertIn("tool_execution_error", issues)
            self.assertEqual(
                [item["status"] for item in trace], ["completed", "error", "error"]
            )

    def test_duplicate_native_tool_call_id_is_rejected_before_second_execution(
        self,
    ) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            responses = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "reused-id",
                            "repository_search_readonly",
                            {"query": "total"},
                        )
                    ],
                ),
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "reused-id",
                            "repository_readonly_read",
                            {"path": "src/totals.py"},
                        )
                    ],
                ),
            ]
            agent, llm = self.make_agent(source_types, responses)
            with self.guard_external_effects():
                run_immediate(agent.run("Explain the total function."))
            self.assertEqual(len(llm.calls), 2)
            self.assertEqual(agent.terminal_reason, "duplicate_tool_call_id")
            trace, reads, issues = collect_tool_trace(agent.messages)
            self.assertEqual(len(reads), 0)
            self.assertIn("missing_or_duplicate_tool_call_id", issues)
            self.assertIn("tool_call_without_result", issues)
            self.assertEqual(trace[0]["tool_call_id"], "reused-id")

    def test_unknown_tool_and_workspace_escape_are_rejected_by_real_loop(self) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            unknown_sequence = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "unknown-1",
                            "python_execute",
                            {"code": "raise SystemExit"},
                        )
                    ],
                ),
                messages.assistant_message("The unregistered tool was rejected."),
            ]
            agent, llm = self.make_agent(source_types, unknown_sequence)
            with self.guard_external_effects():
                run_immediate(agent.run("Try the invalid tool"))
            unknown_result = next(
                message.content
                for message in agent.messages
                if message.role == "tool" and message.tool_call_id == "unknown-1"
            )
            self.assertIn("Unknown tool 'python_execute'", unknown_result)
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(agent.terminal_reason, "tool_error")
            self.assertEqual(len(agent.available_tools.tools), 2)

            traversal_sequence = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            "read-escape",
                            "repository_readonly_read",
                            {"path": "../outside.txt", "start_line": 1, "end_line": 1},
                        )
                    ],
                ),
                messages.assistant_message(
                    "The path escaped the workspace and was rejected."
                ),
            ]
            traversal_agent, traversal_llm = self.make_agent(
                source_types, traversal_sequence
            )
            with self.guard_external_effects():
                run_immediate(traversal_agent.run("Read a path outside the workspace"))
            traversal_result = next(
                message.content
                for message in traversal_agent.messages
                if message.role == "tool" and message.tool_call_id == "read-escape"
            )
            self.assertIn("path_outside_root", traversal_result)
            self.assertEqual(len(traversal_llm.calls), 1)
            self.assertEqual(traversal_agent.terminal_reason, "tool_error")

    def test_truncated_tool_response_is_never_executed(self) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            response = messages(
                role="assistant",
                content="partial",
                tool_calls=[
                    tool_call(
                        source_types,
                        "truncated-read",
                        "repository_readonly_read",
                        {"path": "src/totals.py", "start_line": 1, "end_line": 2},
                    )
                ],
            )
            agent, llm = self.make_agent(source_types, [response])
            llm.last_finish_reason = "length"
            with self.guard_external_effects():
                run_immediate(agent.run("Read the total function"))
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(agent.terminal_reason, "model_truncated")
            self.assertFalse(any(message.role == "tool" for message in agent.messages))

    def test_live_profile_rejects_tool_calls_without_complete_finish_reason(
        self,
    ) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            response = messages(
                role="assistant",
                content="partial tool response",
                tool_calls=[
                    tool_call(
                        source_types,
                        "unfinished-read",
                        "repository_readonly_read",
                        {"path": "src/totals.py", "start_line": 1, "end_line": 2},
                    )
                ],
            )
            agent, llm = self.make_agent(source_types, [response])
            llm.last_finish_reason = None
            llm.require_finish_reason = True
            with self.guard_external_effects():
                run_immediate(agent.run("Read the total function"))
            self.assertEqual(agent.terminal_reason, "invalid_tool_response_termination")
            self.assertFalse(any(message.role == "tool" for message in agent.messages))

    def test_real_llm_adapter_preserves_provider_finish_reason(self) -> None:
        with load_real_agent_sources() as source_types:
            llm_module = sys.modules["app.llm"]
            from app.config import LLMSettings

            settings = LLMSettings(
                model="local-test-model",
                base_url="http://127.0.0.1:1/v1",
                api_key="not-a-secret",
                api_type="openai",
                api_version="",
            )

            class FakeCompletions:
                async def create(self, **_kwargs):
                    return type(
                        "Response",
                        (),
                        {
                            "choices": [
                                type(
                                    "Choice",
                                    (),
                                    {
                                        "message": type(
                                            "AssistantMessage",
                                            (),
                                            {"content": "partial", "tool_calls": None},
                                        )(),
                                        "finish_reason": "length",
                                    },
                                )()
                            ],
                            "usage": type(
                                "Usage",
                                (),
                                {"prompt_tokens": 2, "completion_tokens": 1},
                            )(),
                        },
                    )()

            class LocalTokenizer:
                @staticmethod
                def encode(text):
                    return [0] * len(text)

            with patch.object(
                llm_module, "AsyncOpenAI", return_value=object()
            ), patch.object(
                llm_module.tiktoken, "get_encoding", return_value=LocalTokenizer()
            ):
                llm = source_types["LLM"](
                    config_name=f"finish-reason-{uuid.uuid4().hex}",
                    llm_config={"default": settings},
                )
            llm.client = type(
                "Client",
                (),
                {"chat": type("Chat", (), {"completions": FakeCompletions()})()},
            )()

            async def ask():
                return await llm.ask_tool(
                    messages=[source_types["Message"].user_message("read a file")],
                    tools=[],
                    tool_choice="auto",
                )

            message = run_immediate(ask())
            self.assertEqual(message.content, "partial")
            self.assertEqual(llm.last_finish_reason, "length")

    def test_step_budget_and_factory_rejects_unbounded_or_untrusted_agent_classes(
        self,
    ) -> None:
        with load_real_agent_sources() as source_types:
            messages = source_types["Message"]
            repeated = [
                messages(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_call(
                            source_types,
                            f"search-{index}",
                            "repository_search_readonly",
                            {"query": "total"},
                        )
                    ],
                )
                for index in range(1, 4)
            ]
            agent, llm = self.make_agent(source_types, repeated, max_steps=3)
            with self.guard_external_effects():
                result = run_immediate(agent.run("repeat search"))
            self.assertEqual(len(llm.calls), 3)
            self.assertIn("Reached max steps (3)", result)

            with self.assertRaises(RepoError):
                create_readonly_repository_agent(
                    self.workspace,
                    tool_collection_factory=source_types["ToolCollection"],
                    tool_call_agent_class=object,
                    llm=make_scripted_llm(source_types["LLM"], []),
                )
            with self.assertRaises(RepoError):
                self.make_agent(source_types, [], max_steps=13)
            with self.assertRaises(RepoError):
                create_readonly_repository_agent(
                    self.workspace,
                    tool_collection_factory=source_types["ToolCollection"],
                    tool_call_agent_class=source_types["ToolCallAgent"],
                    llm=object(),
                )


if __name__ == "__main__":
    unittest.main()
