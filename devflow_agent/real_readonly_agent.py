"""Bounded local-model entry point and evidence validation for read-only Q&A."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any, Iterable
from urllib.parse import urlsplit

from .answer_quality import (
    build_numeric_correction_prompt,
    evaluate_answer_quality,
    evaluate_arithmetic_claims,
    evaluate_pytest_diagnostic_evidence,
    extract_expected_numeric_claims,
)
from .openmanus_tools import ALLOWED_TOOL_NAMES
from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode


MAX_AGENT_STEPS = 12
MAX_RUN_TIMEOUT_SECONDS = 300
# The checked-in/local profile keeps a 1024-token default, while bounded
# experiments can request a larger final-answer budget explicitly. This is a
# client-side ceiling; the model server context remains authoritative.
DEFAULT_OUTPUT_TOKENS = 1024
MAX_OUTPUT_TOKENS = 1536
_CITATION_PATH = (
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|pyi|md|rst|txt|toml|ini|cfg|json|yaml|yml|xml|html|css|js|jsx|ts|tsx|go|rs|java|kt|c|cc|cpp|h|hpp|sql|sh)"
)
_CITATION_RE = re.compile(
    rf"(?P<opening>\x60)?(?P<path>{_CITATION_PATH})(?P<path_closing>\x60)?"
    rf"(?:\s+lines?\s+|:\s*)(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?(?P<closing>\x60)?"
)

_CITATION_LIKE_RE = re.compile(
    rf"(?P<opening>\x60)?(?P<path>{_CITATION_PATH})(?P<path_closing>\x60)?"
    rf"(?:\s+lines?\s+|:\s*)(?P<range>[^\s,;()]+)(?P<closing>\x60)?"
)


def _citation_fence_is_valid(match: re.Match[str]) -> bool:
    """Accept plain citations or a balanced Markdown code span around them."""
    opening = match.group("opening")
    path_closing = match.group("path_closing")
    closing = match.group("closing")
    if not any((opening, path_closing, closing)):
        return True
    if opening != "\x60":
        return False
    # Models emit either `path` lines 1-3 or `path:1-3`.
    return (path_closing == "\x60" and closing is None) or (
        path_closing is None and closing == "\x60"
    )


class ReadonlyAgentError(Exception):
    """Safe error contract; messages never contain credentials or raw URLs."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class LocalModelProfile:
    model: str
    base_url: str = field(repr=False)
    api_type: str
    api_key: str = field(repr=False)
    endpoint_sanitized: str
    max_tokens: int


class ConservativeLocalTokenizer:
    """Offline byte-length estimate; intentionally avoids tiktoken downloads."""

    @staticmethod
    def encode(text: str) -> list[int]:
        if not text:
            return []
        # Two UTF-8 bytes per estimated token is conservative for common
        # English and CJK source; this is a budget guard, not provider billing.
        count = (len(text.encode("utf-8")) + 1) // 2
        return [0] * max(1, count)


def _parse_local_endpoint(base_url: str) -> tuple[str, int, str]:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ReadonlyAgentError(
            "invalid_model_endpoint", "Local model endpoint is malformed"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReadonlyAgentError(
            "invalid_model_endpoint",
            "Configure a loopback OpenAI-compatible endpoint without embedded credentials or query parameters",
        )
    try:
        parsed_host = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() != "localhost":
            raise ReadonlyAgentError(
                "external_model_endpoint_blocked",
                "Only localhost or an IP loopback model endpoint is allowed by this read-only CLI",
            )
    else:
        if not parsed_host.is_loopback:
            raise ReadonlyAgentError(
                "external_model_endpoint_blocked",
                "Only loopback model endpoints are allowed by this read-only CLI",
            )
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ReadonlyAgentError(
            "model_endpoint_unresolved", "Local model host could not be resolved"
        ) from exc
    if not addresses:
        raise ReadonlyAgentError(
            "model_endpoint_unresolved", "Local model host resolved to no addresses"
        )
    for address in addresses:
        try:
            if not ipaddress.ip_address(address[4][0].split("%", 1)[0]).is_loopback:
                raise ReadonlyAgentError(
                    "external_model_endpoint_blocked",
                    "Only loopback model endpoints are allowed by this read-only CLI",
                )
        except ValueError as exc:
            raise ReadonlyAgentError(
                "model_endpoint_unresolved",
                "Model host did not resolve to an IP address",
            ) from exc
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    authority = display_host if port == default_port else f"{display_host}:{port}"
    sanitized = f"{parsed.scheme}://{authority}{parsed.path}"
    return host, port, sanitized


def load_local_model_profile(project_root: Path) -> LocalModelProfile:
    """Load only an explicit project config and reject hosted endpoints."""
    config_path = project_root / "config" / "config.toml"
    if not config_path.is_file():
        raise ReadonlyAgentError(
            "local_model_config_missing",
            "Create config/config.toml with a local OpenAI-compatible model endpoint; example configs are not used for live runs",
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReadonlyAgentError(
            "invalid_model_config", "Local model configuration could not be parsed"
        ) from exc
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        raise ReadonlyAgentError(
            "invalid_model_config", "The explicit config must contain an [llm] table"
        )
    model = llm.get("model")
    base_url = llm.get("base_url")
    api_type = str(llm.get("api_type", "")).lower()
    api_key = llm.get("api_key", "")
    max_tokens = llm.get("max_tokens", DEFAULT_OUTPUT_TOKENS)
    if not isinstance(model, str) or not model.strip() or not isinstance(base_url, str):
        raise ReadonlyAgentError(
            "invalid_model_config", "The local model name or endpoint is missing"
        )
    if api_type not in {"", "openai", "ollama"}:
        raise ReadonlyAgentError(
            "unsupported_model_api", "Use an OpenAI-compatible local endpoint"
        )
    if not isinstance(api_key, str):
        raise ReadonlyAgentError(
            "invalid_model_config", "The local API key field must be text"
        )
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < 1
    ):
        raise ReadonlyAgentError(
            "invalid_model_config", "The model output budget must be a positive integer"
        )
    _, _, sanitized = _parse_local_endpoint(base_url)
    return LocalModelProfile(
        model=model.strip(),
        base_url=base_url,
        api_type=api_type,
        api_key=api_key,
        endpoint_sanitized=sanitized,
        max_tokens=min(max_tokens, MAX_OUTPUT_TOKENS),
    )


def _message_value(message: Any, key: str, default: Any = None) -> Any:
    value = (
        message.get(key, default)
        if isinstance(message, dict)
        else getattr(message, key, default)
    )
    return getattr(value, "value", value)


def _decode_tool_result(content: str) -> Any:
    raw = (
        content.split("\n", 1)[-1]
        if content.startswith("Observed output of cmd ")
        else content
    )
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        if raw.startswith("Error:"):
            return {"error": "tool_runtime_error", "message": raw[:1000]}
        return {"raw_text": raw}


def collect_tool_trace(
    messages: Iterable[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Pair real assistant calls and tool results; extract read evidence only."""
    trace: list[dict[str, Any]] = []
    issues: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    reads: list[dict[str, Any]] = []
    trusted_read_ids: set[str] = set()
    for message in messages:
        role = _message_value(message, "role", "")
        calls = _message_value(message, "tool_calls", None) or []
        if role == "assistant":
            for call in calls:
                call_id = _message_value(call, "id", "")
                function = _message_value(call, "function", None)
                name = _message_value(function, "name", "") if function else ""
                arguments = (
                    _message_value(function, "arguments", "{}") if function else "{}"
                )
                if not call_id or call_id in by_id:
                    issues.append("missing_or_duplicate_tool_call_id")
                    if call_id in by_id:
                        issues.append("tool_call_without_result")
                    continue
                try:
                    parsed_arguments = json.loads(arguments or "{}")
                except (TypeError, ValueError):
                    parsed_arguments = None
                    issues.append("invalid_tool_call_arguments")
                item = {
                    "tool_call_id": call_id,
                    "tool": name,
                    "arguments": parsed_arguments,
                    "status": "pending",
                }
                if name not in ALLOWED_TOOL_NAMES:
                    issues.append("unregistered_tool_call")
                trace.append(item)
                by_id[call_id] = item
        elif role == "tool":
            call_id = _message_value(message, "tool_call_id", "")
            item = by_id.get(call_id)
            if item is None:
                issues.append("tool_result_without_matching_call")
                continue
            if item["status"] != "pending":
                issues.append("duplicate_or_rejected_tool_result")
                continue
            result = _decode_tool_result(
                str(_message_value(message, "content", "") or "")
            )
            item["result"] = result
            reported_name = _message_value(message, "name", "")
            if reported_name != item["tool"]:
                issues.append("tool_result_name_mismatch")
            if isinstance(result, dict) and result.get("error"):
                recoverable = (
                    result.get("error") == "invalid_line_range"
                    and result.get("recoverable") is True
                )
                item["status"] = "recoverable_error" if recoverable else "error"
                if not recoverable:
                    issues.append("tool_execution_error")
            else:
                item["status"] = "completed"
                if (
                    item["tool"] == "repository_readonly_read"
                    and isinstance(result, dict)
                    and result.get("result_kind") == "trusted_file_read"
                ):
                    required = (
                        "relative_path",
                        "start_line",
                        "end_line",
                        "line_count",
                        "full_file_sha256",
                        "fragment_sha256",
                        "content",
                        "evidence_id",
                    )
                    valid_hash = bool(
                        re.fullmatch(
                            r"[0-9a-fA-F]{64}", str(result.get("full_file_sha256", ""))
                        )
                    )
                    content = result.get("content")
                    fragment_hash = result.get("fragment_sha256")
                    valid_fragment_hash = (
                        isinstance(content, str)
                        and isinstance(fragment_hash, str)
                        and bool(re.fullmatch(r"[0-9a-fA-F]{64}", fragment_hash))
                        and hashlib.sha256(content.encode("utf-8")).hexdigest()
                        == fragment_hash
                    )
                    valid_range = (
                        isinstance(result.get("start_line"), int)
                        and not isinstance(result.get("start_line"), bool)
                        and isinstance(result.get("end_line"), int)
                        and not isinstance(result.get("end_line"), bool)
                        and isinstance(result.get("line_count"), int)
                        and not isinstance(result.get("line_count"), bool)
                        and result["start_line"] > 0
                        and result["end_line"] >= result["start_line"]
                        and result["end_line"] <= result["line_count"]
                    )
                    if (
                        all(key in result for key in required)
                        and isinstance(content, str)
                        and bool(result.get("evidence_id"))
                        and valid_hash
                        and valid_fragment_hash
                        and valid_range
                    ):
                        reads.append(result)
                        trusted_read_ids.add(call_id)
                    else:
                        issues.append("incomplete_read_evidence")
    for index, item in enumerate(trace):
        if item["status"] != "recoverable_error":
            continue
        failed_path = item.get("result", {}).get("relative_path")
        recovered = next(
            (
                later
                for later in trace[index + 1 :]
                if later["tool"] == "repository_readonly_read"
                and later["status"] == "completed"
                and later["tool_call_id"] in trusted_read_ids
                and later.get("result", {}).get("relative_path") == failed_path
            ),
            None,
        )
        if recovered is None:
            item["status"] = "error"
            issues.append("tool_execution_error")
        else:
            item["status"] = "recovered"
            item["recovered_by_tool_call_id"] = recovered["tool_call_id"]
    if any(item["status"] == "pending" for item in trace):
        issues.append("tool_call_without_result")
    return trace, reads, issues


def validate_answer_citations(
    answer: str,
    read_evidence: Iterable[dict[str, Any]],
    *,
    workspace: AuthorizedWorkspace | None = None,
) -> dict[str, Any]:
    """Validate citation syntax, trusted read ranges, fragment hashes, and freshness."""
    evidence_by_path: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for evidence in read_evidence:
        path = evidence.get("relative_path")
        start_line = evidence.get("start_line")
        end_line = evidence.get("end_line")
        line_count = evidence.get("line_count")
        digest = evidence.get("full_file_sha256")
        content = evidence.get("content")
        fragment_digest = evidence.get("fragment_sha256")
        valid = (
            evidence.get("result_kind") == "trusted_file_read"
            and isinstance(evidence.get("evidence_id"), str)
            and bool(evidence.get("evidence_id"))
            and isinstance(path, str)
            and isinstance(start_line, int)
            and not isinstance(start_line, bool)
            and isinstance(end_line, int)
            and not isinstance(end_line, bool)
            and isinstance(line_count, int)
            and not isinstance(line_count, bool)
            and 1 <= start_line <= end_line <= line_count
            and isinstance(digest, str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", digest))
            and isinstance(fragment_digest, str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", fragment_digest))
            and isinstance(content, str)
            and hashlib.sha256(content.encode("utf-8")).hexdigest() == fragment_digest
            and len(content.splitlines()) == end_line - start_line + 1
        )
        if not valid:
            errors.append("invalid_read_evidence")
            continue
        if workspace is not None:
            evidence_workspace_id = evidence.get("workspace_id")
            if (
                evidence_workspace_id
                and evidence_workspace_id != workspace.workspace_id
            ):
                errors.append("citation_evidence_mismatch")
                continue
            try:
                current = workspace.read_text(
                    path,
                    start_line=start_line,
                    end_line=end_line,
                    expected_sha256=digest,
                )
            except RepoError as exc:
                errors.append(
                    "citation_stale_file_version"
                    if exc.code is RepoErrorCode.STALE_EVIDENCE
                    else "citation_source_unavailable"
                )
                continue
            if (
                current.full_file_sha256 != digest
                or current.fragment_sha256 != fragment_digest
                or current.content != content
                or current.workspace_id != workspace.workspace_id
            ):
                errors.append("citation_evidence_mismatch")
                continue
        evidence_by_path.setdefault(path, []).append(
            {
                "start_line": start_line,
                "end_line": end_line,
                "full_file_sha256": digest,
            }
        )

    matches = list(_CITATION_RE.finditer(answer or ""))
    citation_like = list(_CITATION_LIKE_RE.finditer(answer or ""))
    citations: list[dict[str, Any]] = []
    if not evidence_by_path:
        errors.append("no_trusted_read_evidence")
    if not matches:
        errors.append("missing_path_line_citation")
    if len(citation_like) > len(matches):
        errors.append("invalid_citation_format")
    for match in matches:
        if not _citation_fence_is_valid(match):
            errors.append("invalid_citation_format")
            continue
        path = match.group("path")
        start_line = int(match.group("start"))
        end_line = int(match.group("end") or start_line)
        citation = {
            "relative_path": path,
            "start_line": start_line,
            "end_line": end_line,
        }
        citations.append(citation)
        ranges = evidence_by_path.get(path, [])
        if not ranges:
            errors.append("citation_not_read")
            continue
        matching = next(
            (
                evidence
                for evidence in ranges
                if evidence["start_line"]
                <= start_line
                <= end_line
                <= evidence["end_line"]
            ),
            None,
        )
        if matching is None:
            errors.append("citation_outside_read_range")
        else:
            citation["full_file_sha256"] = matching["full_file_sha256"]
    return {
        "valid": not errors,
        "citations": citations,
        "errors": sorted(set(errors)),
    }


def probe_loopback_endpoint(
    profile: LocalModelProfile, timeout_seconds: float = 2.0
) -> None:
    host, port, _ = _parse_local_endpoint(profile.base_url)
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        # One direct, proxy-free probe. Choose IPv4 where available; IPv6 can be
        # configured explicitly by using ::1 in the endpoint URL.
        address = next(
            (item for item in addresses if item[0] == socket.AF_INET), addresses[0]
        )
        with socket.socket(address[0], address[1], address[2]) as client:
            client.settimeout(timeout_seconds)
            client.connect(address[4])
    except OSError as exc:
        raise ReadonlyAgentError(
            "local_model_unreachable",
            "The configured local model endpoint did not accept a TCP connection",
        ) from exc


def bind_single_attempt_model_call(llm: Any, request_timeout_seconds: int) -> None:
    """Disable OpenManus' six-attempt Tenacity wrapper for this bounded run.

    The underlying upstream request implementation is reused through its
    ``__wrapped__`` function, with a finite per-request timeout. The whole-run
    ``asyncio.wait_for`` remains the harder outer deadline.
    """
    decorated = getattr(type(llm), "ask_tool", None)
    one_attempt = getattr(decorated, "__wrapped__", None)
    if not callable(one_attempt):
        raise ReadonlyAgentError(
            "unsupported_llm_retry_adapter",
            "The fixed OpenManus LLM no longer exposes its bounded request implementation",
        )

    async def ask_once(self, *args: Any, **kwargs: Any) -> Any:
        try:
            requested = int(kwargs.get("timeout", request_timeout_seconds))
        except (TypeError, ValueError):
            requested = request_timeout_seconds
        kwargs["timeout"] = min(max(1, requested), request_timeout_seconds)
        return await one_attempt(self, *args, **kwargs)

    llm.ask_tool = MethodType(ask_once, llm)


async def _close_partial_model_client(llm: Any, http_client: Any | None) -> None:
    """Close a partially initialized SDK/client pair before returning an error."""
    sdk_client = getattr(llm, "client", None)
    if sdk_client is not None:
        close = getattr(sdk_client, "close", None)
    else:
        close = getattr(http_client, "aclose", None)
    if not callable(close):
        if sdk_client is not None or http_client is not None:
            raise ReadonlyAgentError(
                "local_client_cleanup_failed",
                "A partially initialized local model client could not be closed",
            )
        return
    try:
        await close()
    except Exception as exc:
        raise ReadonlyAgentError(
            "local_client_cleanup_failed",
            f"A partially initialized local model client could not be closed ({type(exc).__name__})",
        ) from exc


async def _run_bounded_agent_correction(
    agent: Any,
    prompt: str,
    *,
    step_budget: int,
) -> int:
    """Continue the same read-only OpenManus agent for a bounded correction."""
    from app.schema import AgentState

    if (
        isinstance(step_budget, bool)
        or not isinstance(step_budget, int)
        or not 1 <= step_budget <= MAX_AGENT_STEPS
    ):
        return 0
    if agent.state is not AgentState.IDLE:
        raise ReadonlyAgentError(
            "agent_not_idle",
            "The read-only agent cannot continue from its current state",
        )
    agent.current_step = 0
    agent.max_steps = step_budget
    agent.terminal_reason = "step_limit"
    agent.update_memory("user", prompt)
    steps = 0
    async with agent.state_context(AgentState.RUNNING):
        while steps < step_budget and agent.state != AgentState.FINISHED:
            agent.current_step += 1
            steps += 1
            await agent.step()
        if agent.state != AgentState.FINISHED:
            agent.terminal_reason = "step_limit"
            agent.state = AgentState.FINISHED
    return steps


async def run_local_readonly_question(
    workspace: AuthorizedWorkspace,
    question: str,
    *,
    max_steps: int = 6,
    timeout_seconds: int = 180,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Run one bounded real-model question against an explicitly local endpoint."""
    if not isinstance(question, str) or not question.strip():
        raise ReadonlyAgentError("invalid_question", "Question must not be empty")
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or not 1 <= max_steps <= MAX_AGENT_STEPS
    ):
        raise ReadonlyAgentError(
            "invalid_step_limit", "Step limit must be between 1 and 12"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= MAX_RUN_TIMEOUT_SECONDS
    ):
        raise ReadonlyAgentError(
            "invalid_timeout", "Run timeout must be between 1 and 300 seconds"
        )
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ReadonlyAgentError(
            "invalid_output_budget",
            f"Output token budget must be between 1 and {MAX_OUTPUT_TOKENS} tokens",
        )

    project_root = Path(__file__).resolve().parents[1]
    profile = load_local_model_profile(project_root)
    effective_max_tokens = (
        profile.max_tokens if max_output_tokens is None else max_output_tokens
    )
    expected_numeric_claims = extract_expected_numeric_claims(question)
    probe_loopback_endpoint(profile)

    # Prevent OpenManus' default file sink from persisting prompts or source
    # excerpts. These variables affect this CLI process only.
    os.environ["DEVFLOW_AGENT_DISABLE_FILE_LOG"] = "1"
    os.environ["DEVFLOW_AGENT_LOG_LEVEL"] = "CRITICAL"

    import httpx
    from openai import AsyncOpenAI

    from app.agent.toolcall import ToolCallAgent
    from app.config import config
    from app.llm import LLM, TokenCounter
    from app.tool.tool_collection import ToolCollection

    from .openmanus_agent import create_readonly_repository_agent

    settings = config.llm["default"].model_copy(
        update={
            "model": profile.model,
            "base_url": profile.base_url,
            "api_key": profile.api_key or "devflow-local",
            "max_tokens": effective_max_tokens,
            "api_type": profile.api_type,
        }
    )
    # Construct the actual OpenManus LLM object without its default tokenizer
    # initialization: tiktoken may download merge tables for unknown local
    # model names. The local path uses a conservative offline estimate instead.
    llm = object.__new__(LLM)
    llm.model = profile.model
    llm.max_tokens = effective_max_tokens
    llm.temperature = settings.temperature
    llm.api_type = profile.api_type
    llm.api_key = profile.api_key or "devflow-local"
    llm.api_version = settings.api_version
    llm.base_url = profile.base_url
    llm.total_input_tokens = 0
    llm.total_completion_tokens = 0
    llm.max_input_tokens = settings.max_input_tokens
    llm.tokenizer = ConservativeLocalTokenizer()
    llm.token_counter = TokenCounter(llm.tokenizer)
    llm.last_finish_reason = None
    llm.require_finish_reason = True
    request_timeout = min(120, timeout_seconds)
    http_client = None
    try:
        http_client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(float(request_timeout)),
        )
        llm.client = AsyncOpenAI(
            # OpenAI-compatible loopback services often do not use auth; the
            # SDK requires a non-empty value even though this stays local.
            api_key=profile.api_key or "devflow-local",
            base_url=profile.base_url,
            timeout=float(request_timeout),
            max_retries=0,
            http_client=http_client,
        )
    except Exception as exc:
        try:
            await _close_partial_model_client(llm, http_client)
        except ReadonlyAgentError as cleanup_error:
            raise ReadonlyAgentError(
                "local_client_cleanup_failed",
                f"Local client setup failed ({type(exc).__name__}) and its resources could not be closed",
            ) from cleanup_error
        raise ReadonlyAgentError(
            "local_client_init_failed",
            f"Local model client initialization failed ({type(exc).__name__})",
        ) from exc
    try:
        bind_single_attempt_model_call(llm, request_timeout)
    except Exception as exc:
        try:
            await _close_partial_model_client(llm, http_client)
        except ReadonlyAgentError as cleanup_error:
            raise ReadonlyAgentError(
                "local_client_cleanup_failed",
                f"Local request adapter setup failed ({type(exc).__name__}) and its resources could not be closed",
            ) from cleanup_error
        raise

    agent = None
    started_at = datetime.now(timezone.utc).isoformat()
    started_monotonic = time.monotonic()
    try:
        agent = create_readonly_repository_agent(
            workspace,
            tool_collection_factory=ToolCollection,
            tool_call_agent_class=ToolCallAgent,
            llm=llm,
            max_steps=max_steps,
        )
        await asyncio.wait_for(agent.run(question), timeout=float(timeout_seconds))
        run_status = "completed"
        failure_reason = None
    except asyncio.TimeoutError:
        run_status = "failed"
        failure_reason = "run_timeout"
    except Exception as exc:
        run_status = "failed"
        failure_reason = "provider_or_runtime_error"
        error_type = type(exc).__name__
    finally:
        await llm.client.close()

    if agent is None:
        citation_check = {
            "valid": False,
            "citations": [],
            "errors": ["no_trusted_read_evidence"],
        }
        numeric_check = evaluate_arithmetic_claims(
            "",
            required=bool(expected_numeric_claims),
            expected_claims=expected_numeric_claims,
            trusted_reads=(),
        )
        diagnostic_check = evaluate_pytest_diagnostic_evidence(
            question, "", (), citation_check
        )
        quality = evaluate_answer_quality(
            answer="",
            run_status="failed",
            terminal_reason="agent_initialization_failed",
            trace_issues=["agent_initialization_failed"],
            read_evidence_count=0,
            citation_check=citation_check,
            numeric_check=numeric_check,
            diagnostic_check=diagnostic_check,
        )
        return {
            "kind": "real_local_readonly_agent_run",
            "status": "failed",
            "reason": "agent_initialization_failed",
            "model": profile.model,
            "endpoint": profile.endpoint_sanitized,
            "error_type": error_type
            if "error_type" in locals()
            else "AgentInitializationError",
            "tool_trace": [],
            "read_evidence_count": 0,
            "citations": citation_check,
            "diagnostic_check": diagnostic_check,
            "answer_quality": quality,
            "quality_correction": {
                "limit": 1,
                "attempts": 0,
                "reason": "agent_initialization_failed",
            },
            "answer": "",
        }

    correction = {"limit": 1, "attempts": 0, "reason": "not_needed"}
    trace, reads, trace_issues = collect_tool_trace(agent.messages)
    final_messages = [
        message
        for message in agent.messages
        if _message_value(message, "role", "") == "assistant"
        and not (_message_value(message, "tool_calls", None) or [])
        and (_message_value(message, "content", "") or "").strip()
    ]
    answer = (
        str(_message_value(final_messages[-1], "content", "")) if final_messages else ""
    )
    citation_check = validate_answer_citations(answer, reads, workspace=workspace)
    numeric_check = evaluate_arithmetic_claims(
        answer,
        required=bool(expected_numeric_claims),
        expected_claims=expected_numeric_claims,
        trusted_reads=reads,
    )
    diagnostic_check = evaluate_pytest_diagnostic_evidence(
        question, answer, reads, citation_check
    )
    quality = evaluate_answer_quality(
        answer=answer,
        run_status=run_status,
        terminal_reason=getattr(agent, "terminal_reason", "unknown"),
        trace_issues=trace_issues,
        read_evidence_count=len(reads),
        citation_check=citation_check,
        numeric_check=numeric_check,
        diagnostic_check=diagnostic_check,
    )
    initial_quality = quality
    initial_model_calls = getattr(agent, "model_calls_seen", 0)
    initial_agent_steps = getattr(agent, "current_step", 0)
    correction["initial_model_calls"] = initial_model_calls
    correction["initial_agent_steps"] = initial_agent_steps

    if quality["status"] == "NUMERIC_MISMATCH" and run_status == "completed":
        remaining_steps = max_steps - initial_agent_steps
        remaining_seconds = timeout_seconds - (time.monotonic() - started_monotonic)
        correction["available_steps"] = max(0, remaining_steps)
        if remaining_steps < 1:
            correction["reason"] = "step_budget_exhausted"
        elif remaining_seconds <= 0:
            correction["reason"] = "time_budget_exhausted"
        else:
            correction_prompt = build_numeric_correction_prompt(numeric_check)
            correction["attempts"] = 1
            correction["reason"] = "attempted"
            try:
                correction_steps = await asyncio.wait_for(
                    _run_bounded_agent_correction(
                        agent,
                        correction_prompt,
                        step_budget=remaining_steps,
                    ),
                    timeout=remaining_seconds,
                )
                correction["steps_used"] = correction_steps
                if getattr(agent, "terminal_reason", "unknown") != "final_answer":
                    run_status = "failed"
                    failure_reason = f"quality_correction_{getattr(agent, 'terminal_reason', 'incomplete')}"
                    correction["reason"] = failure_reason
            except asyncio.TimeoutError:
                run_status = "failed"
                failure_reason = "quality_correction_timeout"
                correction["reason"] = failure_reason
            except Exception as exc:
                run_status = "failed"
                failure_reason = "quality_correction_error"
                correction["reason"] = failure_reason
                error_type = type(exc).__name__

        trace, reads, trace_issues = collect_tool_trace(agent.messages)
        final_messages = [
            message
            for message in agent.messages
            if _message_value(message, "role", "") == "assistant"
            and not (_message_value(message, "tool_calls", None) or [])
            and (_message_value(message, "content", "") or "").strip()
        ]
        answer = (
            str(_message_value(final_messages[-1], "content", ""))
            if final_messages
            else ""
        )
        citation_check = validate_answer_citations(answer, reads, workspace=workspace)
        numeric_check = evaluate_arithmetic_claims(
            answer,
            required=bool(expected_numeric_claims),
            expected_claims=expected_numeric_claims,
            trusted_reads=reads,
        )
        diagnostic_check = evaluate_pytest_diagnostic_evidence(
            question, answer, reads, citation_check
        )
        quality = evaluate_answer_quality(
            answer=answer,
            run_status=run_status,
            terminal_reason=getattr(agent, "terminal_reason", "unknown"),
            trace_issues=trace_issues,
            read_evidence_count=len(reads),
            citation_check=citation_check,
            numeric_check=numeric_check,
            diagnostic_check=diagnostic_check,
        )
        correction["initial_quality_status"] = initial_quality["status"]
        correction["final_quality_status"] = quality["status"]
        if correction["reason"] == "attempted" and quality["status"] == "PASS":
            correction["reason"] = "corrected_and_verified_within_supported_checks"
        elif correction["reason"] == "attempted" and quality["status"] != "PASS":
            correction["reason"] = "correction_did_not_pass_quality_gate"

    status = "verified" if quality["status"] == "PASS" else "failed"
    reason = None if status == "verified" else quality["status"].lower()
    if run_status == "failed" and failure_reason:
        reason = failure_reason
    result: dict[str, Any] = {
        "kind": "real_local_readonly_agent_run",
        "status": status,
        "reason": reason,
        "model": profile.model,
        "endpoint": profile.endpoint_sanitized,
        "step_limit": max_steps,
        "request_timeout_seconds": request_timeout,
        "run_timeout_seconds": timeout_seconds,
        "max_output_tokens": effective_max_tokens,
        "started_at_utc": started_at,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "model_calls": getattr(agent, "model_calls_seen", 0),
        "tool_error_recoveries_used": getattr(agent, "tool_error_recoveries_used", 0),
        "tool_error_recovery_limit": 1,
        "quality_correction": correction,
        "finish_reason": getattr(llm, "last_finish_reason", None),
        "allowed_tools": sorted(ALLOWED_TOOL_NAMES),
        "terminal_reason": getattr(agent, "terminal_reason", "unknown"),
        "tool_trace": trace,
        "tool_trace_issues": trace_issues,
        "read_evidence_count": len(reads),
        "citations": citation_check,
        "diagnostic_check": diagnostic_check,
        "answer_quality": quality,
        "answer": answer,
    }
    if (
        run_status == "failed"
        and failure_reason
        and (
            failure_reason == "provider_or_runtime_error"
            or failure_reason.endswith("_error")
        )
    ):
        result["error_type"] = (
            error_type if "error_type" in locals() else "RuntimeError"
        )
    return result
