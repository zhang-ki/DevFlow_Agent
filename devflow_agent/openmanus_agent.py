"""Bounded adapter for OpenManus' real read-only ToolCallAgent loop.

OpenManus package initializers eagerly import broad tool profiles. This module
therefore accepts the exact upstream ToolCallAgent class from the caller,
mirroring the explicit ToolCollection injection in ``openmanus_tools``. It
never imports a default agent profile or adds tools itself beyond the two
read-only repository tools.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import Field

from .openmanus_tools import create_openmanus_readonly_collection
from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode


MAX_READONLY_AGENT_STEPS = 12
MAX_READONLY_TOOL_ERROR_RECOVERIES = 1
COMPACT_DIAGNOSTIC_RESPONSE_RULES = (
    "For CI diagnosis questions, keep the final answer compact: use the fields "
    "Summary, Root cause, Evidence, Confidence, and Unconfirmed. List at most "
    "three evidence bullets, each as path:line-range plus the verified file "
    "SHA-256. Do not repeat tool JSON or source code, and do not emit fenced "
    "code blocks. Finish after the compact report."
)


def create_readonly_repository_agent(
    workspace: AuthorizedWorkspace,
    *,
    tool_collection_factory: Callable[[], Any],
    tool_call_agent_class: type,
    llm: Any,
    max_steps: int = 8,
) -> Any:
    """Create a bounded OpenManus ToolCallAgent with an exact read-only allowlist.

    The caller must pass the fixed ``app.agent.toolcall.ToolCallAgent`` class,
    the fixed ``app.tool.tool_collection.ToolCollection`` constructor and an
    LLM instance accepted by that class. The LLM may be a deterministic test
    double; this factory does not make model or network requests itself.

    The OpenManus ToolCallAgent normally finishes through a registered
    ``terminate`` tool. This profile intentionally excludes that extra tool,
    so its narrow subclass treats a final assistant response with no tool call
    as completion. Tool decisions and executions continue through the actual
    upstream ``think``, ``execute_tool``, ``ToolCollection.execute`` and
    ``act`` implementations.
    """

    if (
        not isinstance(tool_call_agent_class, type)
        or tool_call_agent_class.__name__ != "ToolCallAgent"
        or tool_call_agent_class.__module__ != "app.agent.toolcall"
    ):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "an explicit fixed OpenManus ToolCallAgent class is required",
        )
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or not 1 <= max_steps <= MAX_READONLY_AGENT_STEPS
    ):
        raise RepoError(
            RepoErrorCode.INVALID_LIMIT,
            "agent step limit must be between 1 and 12",
            details={"max_steps": MAX_READONLY_AGENT_STEPS},
        )

    collection = create_openmanus_readonly_collection(
        workspace, tool_collection_factory
    )

    # These are imported only after the caller has supplied and validated the
    # upstream class, avoiding a default OpenManus agent/tool package import.
    from app.llm import LLM
    from app.schema import AgentState, ToolChoice

    if not isinstance(llm, LLM):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "an explicit OpenManus LLM instance is required; "
            "implicit provider creation is disabled",
        )

    class ReadOnlyRepositoryAgent(tool_call_agent_class):
        name: str = "devflow_repository_readonly"
        description: str = "A bounded agent for evidence-based repository reading."
        system_prompt: str = (
            "Answer repository questions using only the registered read-only "
            "repository tools. Search results are candidates, not evidence; "
            "read a file before citing it. Search results include the actual "
            "line_count and indexed_sha256 from a fresh index snapshot, but are "
            "not read evidence. Use 1-based inclusive line ranges within that "
            "line_count, or omit the range for a bounded preview. If a read "
            "returns a recoverable invalid_line_range error, use its line_count "
            "and max_lines details to correct the call once; do not repeat the "
            "same invalid range. This ask allows at most one such recovery. "
            "Emit exactly one tool call in each assistant decision; wait for "
            "that tool result before selecting another tool. "
            "Cite only verified relative paths and lines from successful reads. "
            "Never claim a file was read unless the read tool returned its contents. "
            + COMPACT_DIAGNOSTIC_RESPONSE_RULES
        )
        next_step_prompt: str | None = None
        special_tool_names: list[str] = []
        max_steps: int = 8
        terminal_reason: str = "step_limit"
        model_calls_seen: int = 0
        tool_error_recoveries_used: int = 0
        seen_tool_call_ids: set[str] = Field(default_factory=set)

        async def think(self) -> bool:
            self.model_calls_seen += 1
            try:
                should_act = await super().think()
            except Exception:
                self.terminal_reason = "provider_error"
                self.state = AgentState.FINISHED
                raise

            finish_reason = getattr(self.llm, "last_finish_reason", None)
            if finish_reason == "length":
                self.terminal_reason = "model_truncated"
                self.state = AgentState.FINISHED
                return False
            if finish_reason == "content_filter":
                self.terminal_reason = "model_response_rejected"
                self.state = AgentState.FINISHED
                return False
            if getattr(self.llm, "require_finish_reason", False):
                if self.tool_calls and finish_reason != "tool_calls":
                    self.terminal_reason = "invalid_tool_response_termination"
                    self.state = AgentState.FINISHED
                    return False
                if not self.tool_calls and finish_reason != "stop":
                    self.terminal_reason = "incomplete_model_response"
                    self.state = AgentState.FINISHED
                    return False
            elif self.tool_calls and finish_reason not in (None, "tool_calls"):
                self.terminal_reason = "invalid_tool_response_termination"
                self.state = AgentState.FINISHED
                return False
            if not should_act:
                last_content = self.messages[-1].content if self.messages else ""
                self.terminal_reason = (
                    "context_limit"
                    if last_content and last_content.startswith("Maximum token limit reached")
                    else "empty_or_invalid_model_response"
                )
                self.state = AgentState.FINISHED
            return should_act

        async def act(self) -> str:
            if not self.tool_calls:
                answer = self.messages[-1].content or ""
                self.state = AgentState.FINISHED
                if answer.strip():
                    self.terminal_reason = "final_answer"
                    return answer
                self.terminal_reason = "empty_final_answer"
                return "No final answer was provided."
            if len(self.tool_calls) != 1:
                self.terminal_reason = "multiple_tool_calls_rejected"
                self.state = AgentState.FINISHED
                return "Error: exactly one read-only tool call is allowed per model decision"

            call = self.tool_calls[0]
            call_id = getattr(call, "id", None)
            function = getattr(call, "function", None)
            tool_name = getattr(function, "name", None)
            if not isinstance(call_id, str) or not call_id.strip():
                self.terminal_reason = "missing_tool_call_id"
                self.state = AgentState.FINISHED
                return "Error: tool call has no usable call ID"
            if call_id in self.seen_tool_call_ids:
                self.terminal_reason = "duplicate_tool_call_id"
                self.state = AgentState.FINISHED
                return "Error: duplicate tool call ID rejected"
            self.seen_tool_call_ids.add(call_id)

            previous_message_count = len(self.messages)
            result = await super().act()
            new_messages = self.messages[previous_message_count:]
            if (
                not new_messages
                or new_messages[-1].role != "tool"
                or new_messages[-1].tool_call_id != call_id
                or new_messages[-1].name != tool_name
            ):
                self.terminal_reason = "tool_result_protocol_error"
                self.state = AgentState.FINISHED
            return result

        async def execute_tool(self, command: Any) -> str:
            result = await super().execute_tool(command)
            is_error = result.startswith("Error:")
            payload = None
            if not is_error:
                raw = result.split("\n", 1)[-1]
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    payload = None
                is_error = isinstance(payload, dict) and bool(payload.get("error"))

            if is_error:
                is_recoverable_range = (
                    isinstance(payload, dict)
                    and payload.get("error") == "invalid_line_range"
                    and payload.get("recoverable") is True
                )
                if (
                    is_recoverable_range
                    and self.tool_error_recoveries_used < MAX_READONLY_TOOL_ERROR_RECOVERIES
                ):
                    self.tool_error_recoveries_used += 1
                    self.terminal_reason = "recoverable_tool_error_observed"
                else:
                    self.terminal_reason = (
                        "tool_error_recovery_limit"
                        if is_recoverable_range
                        else "tool_error"
                    )
                    self.state = AgentState.FINISHED
            return result

    return ReadOnlyRepositoryAgent(
        llm=llm,
        available_tools=collection,
        tool_choices=ToolChoice.AUTO,
        special_tool_names=[],
        next_step_prompt=None,
        max_steps=max_steps,
    )
