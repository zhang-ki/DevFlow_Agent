"""Bounded, read-only pytest failure diagnosis.

The diagnostic path consumes an already captured pytest log.  It never runs a
command found in the log and it reuses the same repository search/read tools as
the read-only Q&A path.  A report is useful even when the log is incomplete,
but it marks unsupported conclusions as ``UNVERIFIABLE``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .repository import AuthorizedWorkspace, RepoError
from .tools import RepositoryReadTool, RepositorySearchTool


MAX_LOG_CHARS = 128_000
MAX_FAILURES = 20
MAX_EVIDENCE_LINES = 12
MAX_EVIDENCE_CHARS = 8_000
MAX_COMPACT_EVIDENCE = 3
MAX_COMPACT_UNCERTAINTIES = 3

_FAILED_RE = re.compile(
    r"^FAILED\s+(?P<nodeid>\S+?)(?:\s+-\s+(?P<message>.*))?$",
    re.MULTILINE,
)
_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|\.?[\\/])?[^:\r\n]+?\.py):(?P<line>[1-9][0-9]*)(?::\s*(?:in\s+)?[^\r\n]*)?",
    re.IGNORECASE,
)
_ERROR_LINE_RE = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)
_EXCEPTION_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure))\b"
)
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass(frozen=True)
class FailureEvent:
    """A normalized failure extracted from pytest output."""

    nodeid: str
    test_name: str
    test_path: str | None
    test_line: int | None
    source_path: str | None
    source_line: int | None
    exception_type: str | None
    message: str
    excerpt: str


@dataclass(frozen=True)
class ParsedPytestLog:
    failures: tuple[FailureEvent, ...]
    exit_code: int | None
    truncated: bool
    warnings: tuple[str, ...] = ()


@dataclass
class DiagnosticReport:
    schema_version: int
    status: str
    exit_code: int | None
    parsed_failures: list[dict[str, Any]]
    metrics: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    log_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": "devflow_ci_diagnostic_report",
            "status": self.status,
            "exit_code": self.exit_code,
            "parsed_failures": self.parsed_failures,
            "metrics": self.metrics,
            "warnings": self.warnings,
            "log_excerpt": self.log_excerpt,
            "read_only": True,
            "commands_executed": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def to_compact_dict(self) -> dict[str, Any]:
        """Return the bounded answer contract used by model-facing reports.

        This is intentionally a projection of trusted report data. It never
        copies full tool output or source excerpts, and it caps evidence so a
        final model response cannot consume the entire context window.
        """
        failures: list[dict[str, Any]] = []
        for item in self.parsed_failures:
            root = item.get("root_cause") or {}
            evidence = []
            for evidence_item in (item.get("evidence") or [])[:MAX_COMPACT_EVIDENCE]:
                evidence.append(
                    {
                        "relative_path": evidence_item.get("relative_path"),
                        "start_line": evidence_item.get("start_line"),
                        "end_line": evidence_item.get("end_line"),
                        "full_file_sha256": evidence_item.get("full_file_sha256"),
                        "summary": evidence_item.get("summary", "")[:240],
                    }
                )
            failures.append(
                {
                    "test_nodeid": item.get("test_nodeid"),
                    "failure_summary": item.get("message") or "UNVERIFIABLE",
                    "candidate_root_cause": root.get("classification", "UNVERIFIABLE"),
                    "confidence": root.get("confidence", "low"),
                    "test_location": item.get("test_location"),
                    "source_location": item.get("source_location"),
                    "evidence": evidence,
                    "unconfirmed": list(item.get("uncertainties") or [])[
                        :MAX_COMPACT_UNCERTAINTIES
                    ],
                }
            )
        return {
            "schema_version": 1,
            "kind": "devflow_compact_ci_diagnostic",
            "status": self.status,
            "exit_code": self.exit_code,
            "failures": failures,
            "read_only": True,
            "commands_executed": False,
        }

    def to_compact_json(self) -> str:
        return json.dumps(
            self.to_compact_dict(), ensure_ascii=False, indent=2, sort_keys=True
        )

    def to_compact_markdown(self) -> str:
        lines = [
            "# Compact CI diagnosis",
            "",
            f"- Status: **{self.status}**",
            f"- Exit code: `{self.exit_code}`",
        ]
        for failure in self.to_compact_dict()["failures"]:
            lines.extend(
                [
                    "",
                    f"## {failure.get('test_nodeid') or 'failure'}",
                    "",
                    f"- Summary: {failure.get('failure_summary')}",
                    f"- Root cause: **{failure.get('candidate_root_cause')}**",
                    f"- Confidence: `{failure.get('confidence')}`",
                    "- Evidence:",
                ]
            )
            for evidence in failure.get("evidence", []):
                lines.append(
                    f"  - `{evidence.get('relative_path')}:{evidence.get('start_line')}-{evidence.get('end_line')}` "
                    f"(sha256 `{evidence.get('full_file_sha256')}`): {evidence.get('summary', '')}"
                )
            if failure.get("unconfirmed"):
                lines.append("- Unconfirmed: " + "; ".join(failure["unconfirmed"]))
        return "\n".join(lines) + "\n"

    def to_markdown(self) -> str:
        lines = [
            "# DevFlow CI failure diagnosis",
            "",
            f"- Status: **{self.status}**",
            f"- Pytest exit code: `{self.exit_code}`",
            "- Execution: read-only; commands from the log were not run",
            "",
            "## Summary",
            "",
            f"- Failures parsed: {len(self.parsed_failures)}",
        ]
        for index, failure in enumerate(self.parsed_failures, 1):
            lines.extend(
                [
                    "",
                    f"## Failure {index}: `{failure.get('test_nodeid') or 'unknown'}`",
                    "",
                    f"- Exception: `{failure.get('exception_type') or 'UNKNOWN'}`",
                    f"- Message: {failure.get('message') or 'UNVERIFIABLE'}",
                    f"- Test location: `{_location_text(failure.get('test_location'))}`",
                    f"- Candidate source: `{_location_text(failure.get('source_location'))}`",
                    f"- Root-cause classification: **{failure.get('root_cause', {}).get('classification', 'UNVERIFIABLE')}**",
                    f"- Confidence: `{failure.get('root_cause', {}).get('confidence', 'low')}`",
                ]
            )
            evidence = failure.get("evidence") or []
            if evidence:
                lines.extend(["", "### Evidence"])
                for item in evidence:
                    lines.append(
                        f"- `{item.get('relative_path')}:{item.get('start_line')}-{item.get('end_line')}` "
                        f"(sha256 `{item.get('full_file_sha256')}`): {item.get('summary', '')}"
                    )
            uncertainties = failure.get("uncertainties") or []
            if uncertainties:
                lines.extend(["", "### Unconfirmed", ""])
                lines.extend(f"- {item}" for item in uncertainties)
        if self.warnings:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {item}" for item in self.warnings)
        return "\n".join(lines) + "\n"


def _redact(value: str, limit: int = 2_000) -> str:
    cleaned = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return cleaned[:limit]


def _path_from_nodeid(nodeid: str) -> str | None:
    value = nodeid.split("::", 1)[0].strip()
    return value.replace("\\", "/") if value else None


def _test_name_from_nodeid(nodeid: str) -> str:
    parts = nodeid.split("::")
    return "::".join(parts[1:]) if len(parts) > 1 else parts[0]


def _relative_candidate(workspace: AuthorizedWorkspace, raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().strip("()[]")
    value = value.replace("\\", "/")
    try:
        path = Path(value)
        if path.is_absolute():
            return path.resolve(strict=False).relative_to(workspace.root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return value.lstrip("./")


def _classify_exception(exception_type: str | None, message: str) -> str:
    name = (exception_type or "").casefold()
    if name == "assertionerror" or "assert" in message.casefold():
        return "assertion_mismatch"
    if name == "typeerror":
        return "type_contract_mismatch"
    if name in {"attributeerror", "keyerror", "indexerror"}:
        return "missing_or_invalid_value_access"
    if name == "valueerror":
        return "invalid_value_or_validation_path"
    if name in {"zerodivisionerror", "overflowerror"}:
        return "arithmetic_boundary_error"
    return "UNVERIFIABLE"


def parse_pytest_log(log_text: str, *, exit_code: int | None = None) -> ParsedPytestLog:
    """Parse bounded pytest output without interpreting or executing it."""
    if not isinstance(log_text, str):
        return ParsedPytestLog((), exit_code, False, ("log_not_text",))
    truncated = len(log_text) > MAX_LOG_CHARS
    text = log_text[:MAX_LOG_CHARS]
    warnings: list[str] = []
    if truncated:
        warnings.append("log_truncated_at_limit")
    summaries = list(_FAILED_RE.finditer(text))[:MAX_FAILURES]
    locations = list(_LOCATION_RE.finditer(text))
    errors = [
        _redact(item.group("message").strip()) for item in _ERROR_LINE_RE.finditer(text)
    ]
    failures: list[FailureEvent] = []
    for index, summary in enumerate(summaries):
        nodeid = summary.group("nodeid")
        test_path = _path_from_nodeid(nodeid)
        test_location = next(
            (
                item
                for item in locations
                if _path_from_nodeid(item.group("path")) == test_path
            ),
            None,
        )
        source_location = next(
            (
                item
                for item in locations
                if _path_from_nodeid(item.group("path")) != test_path
            ),
            None,
        )
        message = _redact(
            summary.group("message") or (errors[index] if index < len(errors) else "")
        )
        combined = " ".join([message, " ".join(errors)])
        exception = next(
            (m.group("name") for m in _EXCEPTION_RE.finditer(combined)), None
        )
        start = summary.start()
        end = summaries[index + 1].start() if index + 1 < len(summaries) else len(text)
        excerpt = _redact(text[start:end].strip(), 4_000)
        failures.append(
            FailureEvent(
                nodeid=nodeid,
                test_name=_test_name_from_nodeid(nodeid),
                test_path=test_path,
                test_line=int(test_location.group("line")) if test_location else None,
                source_path=_path_from_nodeid(source_location.group("path"))
                if source_location
                else None,
                source_line=int(source_location.group("line"))
                if source_location
                else None,
                exception_type=exception,
                message=message or "pytest failure did not include a message",
                excerpt=excerpt,
            )
        )
    if not failures and exit_code not in (None, 0):
        warnings.append("no_structured_failed_summary")
    if len(summaries) > MAX_FAILURES:
        warnings.append("failure_count_truncated_at_limit")
    return ParsedPytestLog(
        tuple(failures), exit_code, truncated, tuple(sorted(set(warnings)))
    )


def _search_query(failure: FailureEvent) -> str:
    words = _WORD_RE.findall(
        " ".join((failure.test_name, failure.exception_type or "", failure.message))
    )
    return " ".join(dict.fromkeys(words))[:240] or "pytest failure"


async def diagnose_failures_async(
    workspace: AuthorizedWorkspace,
    log_text: str,
    *,
    exit_code: int | None = None,
) -> DiagnosticReport:
    parsed = parse_pytest_log(log_text, exit_code=exit_code)
    search_tool = RepositorySearchTool(workspace)
    read_tool = RepositoryReadTool(workspace)
    reports: list[dict[str, Any]] = []
    top1_hits = 0
    evidence_count = 0
    for failure in parsed.failures:
        candidates: list[dict[str, Any]] = []
        query = _search_query(failure)
        try:
            search_result = await search_tool.execute(query=query, limit=5)
            candidates = list(search_result.get("results") or [])
        except (RepoError, TypeError, ValueError):
            candidates = []
        candidate_paths = [
            item.get("relative_path")
            for item in candidates
            if item.get("relative_path")
        ]
        direct_path = _relative_candidate(workspace, failure.source_path)
        if direct_path and direct_path not in candidate_paths:
            candidate_paths.insert(0, direct_path)
        evidence: list[dict[str, Any]] = []
        source_location: dict[str, Any] | None = None
        for candidate_path in candidate_paths[:5]:
            if not isinstance(candidate_path, str):
                continue
            line = (
                failure.source_line
                if candidate_path == failure.source_path
                else failure.test_line
            )
            start_line = max(1, (line or 1) - 3)
            (line or 1) + 4
            try:
                read_result = await read_tool.execute(
                    path=candidate_path,
                    start_line=start_line,
                )
            except (RepoError, TypeError, ValueError):
                continue
            if "error" in read_result:
                continue
            content = str(read_result.get("content", ""))
            content = content[:MAX_EVIDENCE_CHARS]
            item = {
                "relative_path": read_result.get("relative_path"),
                "start_line": read_result.get("start_line"),
                "end_line": read_result.get("end_line"),
                "full_file_sha256": read_result.get("full_file_sha256"),
                "fragment_sha256": read_result.get("fragment_sha256"),
                "summary": _redact(" ".join(content.splitlines())[:500]),
                "evidence_id": read_result.get("evidence_id"),
            }
            evidence.append(item)
            if source_location is None:
                source_location = {
                    "relative_path": item["relative_path"],
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                }
            if len(evidence) >= 2:
                break
        if direct_path and candidate_paths and candidate_paths[0] == direct_path:
            top1_hits += 1
        evidence_count += len(evidence)
        classification = _classify_exception(failure.exception_type, failure.message)
        source_hint_available = bool(failure.source_path)
        confidence = "medium" if evidence and source_hint_available else "low"
        uncertainties = [
            "The log and bounded source evidence identify a candidate location; they do not prove causality without rerunning the test.",
        ]
        if not source_hint_available:
            uncertainties.insert(
                0,
                "The log did not identify an implementation source frame; test output alone is insufficient to prove a root cause.",
            )
        if not evidence:
            uncertainties.insert(
                0,
                "No trusted source read could be obtained inside the authorized workspace.",
            )
        reports.append(
            {
                "test_nodeid": failure.nodeid,
                "test_name": failure.test_name,
                "exception_type": failure.exception_type,
                "message": failure.message,
                "test_location": {
                    "relative_path": failure.test_path,
                    "line": failure.test_line,
                },
                "source_location": source_location,
                "search_query": query,
                "search_candidates": candidates[:5],
                "evidence": evidence,
                "root_cause": {
                    "classification": classification
                    if evidence and source_hint_available
                    else "UNVERIFIABLE",
                    "confidence": confidence,
                    "status": "CANDIDATE"
                    if evidence and source_hint_available
                    else "UNVERIFIABLE",
                },
                "uncertainties": uncertainties,
                "log_excerpt": failure.excerpt,
            }
        )
    if not parsed.failures:
        status = "UNVERIFIABLE"
    elif evidence_count == 0:
        status = "UNVERIFIABLE"
    elif any(
        item.get("root_cause", {}).get("status") == "UNVERIFIABLE" for item in reports
    ):
        status = "PARTIAL"
    else:
        status = "PASS"
    metrics = {
        "failures_parsed": len(parsed.failures),
        "top1_file_candidates": top1_hits,
        "top1_file_rate": (top1_hits / len(parsed.failures))
        if parsed.failures
        else None,
        "trusted_evidence_items": evidence_count,
        "commands_executed": 0,
        "read_only_tools": ["repository_search_readonly", "repository_readonly_read"],
    }
    warnings = list(parsed.warnings)
    return DiagnosticReport(
        schema_version=1,
        status=status,
        exit_code=parsed.exit_code,
        parsed_failures=reports,
        metrics=metrics,
        warnings=warnings,
        log_excerpt=_redact(log_text[:4_000]),
    )


def diagnose_failures(
    workspace: AuthorizedWorkspace,
    log_text: str,
    *,
    exit_code: int | None = None,
) -> DiagnosticReport:
    """Synchronous CLI/test wrapper around the bounded async tool path."""
    return asyncio.run(
        diagnose_failures_async(workspace, log_text, exit_code=exit_code)
    )


def evaluate_diagnostic_case(
    report: DiagnosticReport, expected: dict[str, Any]
) -> dict[str, Any]:
    """Compare a report with a fixed gold case without changing the report."""
    failures = report.parsed_failures
    expected_path = expected.get("expected_source_path")
    expected_test = expected.get("expected_test_nodeid")
    expected_exception = expected.get("expected_exception_type")
    top_paths = [
        item.get("source_location", {}).get("relative_path")
        for item in failures
        if item.get("source_location")
    ]
    return {
        "file_top1": bool(top_paths and top_paths[0] == expected_path),
        "file_top3": expected_path in top_paths[:3],
        "test_localized": any(
            item.get("test_nodeid") == expected_test for item in failures
        ),
        "exception_classified": any(
            item.get("exception_type") == expected_exception for item in failures
        ),
        "evidence_paths": sorted(
            {
                path
                for item in failures
                for path in [
                    evidence.get("relative_path")
                    for evidence in item.get("evidence", [])
                ]
                if path
            }
        ),
        "report_status": report.status,
    }


def _location_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "UNVERIFIABLE"
    path = value.get("relative_path") or "unknown"
    line = value.get("line")
    return f"{path}:{line}" if line else str(path)
