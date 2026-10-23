"""Low-dependency, read-only adapters for OpenManus-style tool collections.

This module deliberately does not import app.tool or any optional OpenManus
dependency. Pass the returned objects to a ToolCollection already created by a
caller that has chosen its tool profile.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode, build_index


class ToolRegistrar(Protocol):
    tools: tuple[Any, ...]

    def add_tools(self, *tools: Any) -> Any: ...


class _ReadOnlyTool:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_param(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def __call__(self, **kwargs: Any) -> str:
        try:
            result = await self.execute(**kwargs)
            rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
            workspace = getattr(self, "workspace")
            if len(rendered.encode("utf-8")) > workspace.limits.max_return_bytes:
                return json.dumps(
                    {
                        "error": "return_limit_exceeded",
                        "message": "tool result exceeds the configured output limit",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            return rendered
        except RepoError as exc:
            payload = exc.as_dict()
            if exc.code is RepoErrorCode.INVALID_LINE_RANGE:
                payload["recoverable"] = True
                payload["recovery_guidance"] = (
                    "Use the reported line_count and max_lines with 1-based inclusive "
                    "line numbers, or omit the range for a bounded preview."
                )
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            return json.dumps(
                {"error": "invalid_tool_arguments", "cause_type": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class RepositorySearchTool(_ReadOnlyTool):
    name = "repository_search_readonly"
    description = (
        "Search source paths and Python symbols. Each result includes line_count "
        "and indexed_sha256 from a fresh index of actual file bytes; these are "
        "candidates only, not trusted read evidence. Reading rechecks current bytes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path_filter": {"type": ["string", "null"]},
            "symbol_filter": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: AuthorizedWorkspace) -> None:
        self.workspace = workspace

    async def execute(
        self,
        *,
        query: str,
        path_filter: str | None = None,
        symbol_filter: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        index = build_index(self.workspace)
        return {
            "result_kind": "index_candidates_not_read_evidence",
            "stats": {
                "scanned_files": index.stats.scanned_files,
                "indexed_files": index.stats.indexed_files,
                "indexed_symbols": index.stats.indexed_symbols,
                "truncated": index.stats.truncated,
            },
            "results": [
                {
                    "relative_path": hit.relative_path,
                    "score": hit.score,
                    "matched_terms": hit.matched_terms,
                    "matched_line_numbers": hit.matched_line_numbers,
                    "matched_symbols": hit.matched_symbols,
                    "indexed_sha256": hit.indexed_sha256,
                    "line_count": hit.line_count,
                    "line_count_source": "fresh_index_snapshot",
                    "read_evidence": False,
                }
                for hit in index.search(
                    query,
                    path_filter=path_filter,
                    symbol_filter=symbol_filter,
                    limit=limit,
                )
            ],
        }


class RepositoryReadTool(_ReadOnlyTool):
    name = "repository_readonly_read"
    description = (
        "Read a UTF-8 text file inside the authorized workspace. Explicit ranges "
        "are 1-based and inclusive and must satisfy 1 <= start_line <= end_line "
        "<= the actual line_count, with at most 500 lines. If the range is omitted, "
        "a bounded preview starts at line 1 (up to 500 lines and 64 KiB); the "
        "result reports its actual range, total line_count, and whether it was "
        "truncated. An invalid explicit range returns recoverable line_count and "
        "max_lines details; one correction is allowed per ask. Every successful "
        "read rechecks the current file and returns its full SHA-256."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path from a search result.",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional 1-based inclusive first line; defaults to 1.",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional 1-based inclusive last line. Omit it for a bounded "
                    "preview; an explicitly invalid range is rejected."
                ),
            },
            "expected_sha256": {
                "type": ["string", "null"],
                "description": (
                    "Optional indexed_sha256 from search; stale file versions are rejected."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: AuthorizedWorkspace) -> None:
        self.workspace = workspace

    async def execute(
        self,
        *,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        evidence = self.workspace.read_text(
            path,
            start_line=1 if start_line is None else start_line,
            end_line=end_line,
            expected_sha256=expected_sha256,
        )
        return {
            "result_kind": evidence.evidence_kind,
            "evidence_id": evidence.evidence_id,
            "workspace_id": evidence.workspace_id,
            "relative_path": evidence.relative_path,
            "start_line": evidence.start_line,
            "end_line": evidence.end_line,
            "line_count": evidence.line_count,
            "truncated": evidence.truncated,
            "range_mode": "bounded_default" if end_line is None else "explicit",
            "content": evidence.content,
            "full_file_sha256": evidence.full_file_sha256,
            "fragment_sha256": evidence.fragment_sha256,
            "file_size_bytes": evidence.file_size_bytes,
            "task_id": evidence.task_id,
            "run_id": evidence.run_id,
        }


def register_read_only_tools(
    collection: ToolRegistrar,
    workspace: AuthorizedWorkspace,
) -> tuple[RepositorySearchTool, RepositoryReadTool]:
    """Register only into an empty, explicitly allowlisted tool collection."""
    existing = getattr(collection, "tools", None)
    if existing is None:
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "registry does not expose its current tool set for allowlist validation",
        )
    if tuple(existing):
        raise RepoError(
            RepoErrorCode.UNSAFE_REGISTRY_PROFILE,
            "read-only adapters require an empty tool collection",
        )
    tools = (RepositorySearchTool(workspace), RepositoryReadTool(workspace))
    collection.add_tools(*tools)
    return tools
