"""Bounded, evidence-driven replanning over the existing M4 workflow.

M5 is a control-plane coordinator.  It does not generate patches itself and it
does not register a write-capable Agent tool.  Each candidate is supplied by a
planner, bound to the latest failure evidence, and executed through a fresh M4
``RepairWorkflow`` attempt with its own explicit approval and one-shot ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .repair_workflow import (
    RepairWorkflow,
    WorkflowError,
    WorkflowLimits,
    WorkflowState,
)
from .repository import AuthorizedWorkspace, RepoError, build_index


_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s\"']+[\\/])+[^\s\"']*")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?s\b")


class ReplanState(str, Enum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    ATTEMPT_EXECUTING = "ATTEMPT_EXECUTING"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    COMPLETED = "COMPLETED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SAFETY_REJECTED = "SAFETY_REJECTED"
    TEST_TIMEOUT = "TEST_TIMEOUT"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    FAILED = "FAILED"


_TRANSITIONS: dict[ReplanState, set[ReplanState]] = {
    ReplanState.CREATED: {
        ReplanState.PLANNING,
        ReplanState.FAILED,
        ReplanState.PERSISTENCE_ERROR,
    },
    ReplanState.PLANNING: {
        ReplanState.APPROVAL_PENDING,
        ReplanState.INSUFFICIENT_EVIDENCE,
        ReplanState.REPEATED_FAILURE,
        ReplanState.SAFETY_REJECTED,
        ReplanState.PERSISTENCE_ERROR,
        ReplanState.RESULT_UNKNOWN,
        ReplanState.FAILED,
    },
    ReplanState.APPROVAL_PENDING: {
        ReplanState.ATTEMPT_EXECUTING,
        ReplanState.APPROVAL_REQUIRED,
        ReplanState.APPROVAL_REJECTED,
        ReplanState.PERSISTENCE_ERROR,
        ReplanState.FAILED,
    },
    ReplanState.ATTEMPT_EXECUTING: {
        ReplanState.COMPLETED,
        ReplanState.REPLAN_REQUIRED,
        ReplanState.RETRY_EXHAUSTED,
        ReplanState.REPEATED_FAILURE,
        ReplanState.INSUFFICIENT_EVIDENCE,
        ReplanState.SAFETY_REJECTED,
        ReplanState.TEST_TIMEOUT,
        ReplanState.PERSISTENCE_ERROR,
        ReplanState.RESULT_UNKNOWN,
        ReplanState.FAILED,
    },
    ReplanState.REPLAN_REQUIRED: {ReplanState.PLANNING},
}

_TERMINAL_STATES = {
    ReplanState.COMPLETED,
    ReplanState.APPROVAL_REQUIRED,
    ReplanState.APPROVAL_REJECTED,
    ReplanState.RETRY_EXHAUSTED,
    ReplanState.REPEATED_FAILURE,
    ReplanState.INSUFFICIENT_EVIDENCE,
    ReplanState.SAFETY_REJECTED,
    ReplanState.TEST_TIMEOUT,
    ReplanState.PERSISTENCE_ERROR,
    ReplanState.RESULT_UNKNOWN,
    ReplanState.FAILED,
}


class ReplanError(WorkflowError):
    """Stable fail-closed error for the M5 control plane."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _safe_text(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    return text[:limit]


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _failure_signature_text(value: str) -> str:
    """Remove known volatile timing noise without changing failure semantics."""
    return _DURATION_RE.sub("<duration>", value).strip()


def _workspace_snapshot(workspace: AuthorizedWorkspace) -> dict[str, Any]:
    records = sorted(
        build_index(workspace).file_records, key=lambda item: item.relative_path
    )
    digest_input = "\n".join(
        f"{item.relative_path}\0{item.size_bytes}\0{item.sha256}" for item in records
    )
    return {"file_count": len(records), "sha256": _sha256_text(digest_input)}


@dataclass(frozen=True)
class ReplanLimits:
    """Hard bounds for a complete M5 run."""

    max_replans: int = 1
    max_events: int = 24
    test_timeout_seconds: float = 60.0
    max_replacement_bytes: int = 262_144

    def __post_init__(self) -> None:
        if self.max_replans < 0 or self.max_replans > 3:
            raise ValueError("max_replans must be between 0 and 3")
        if self.max_events < 4 or self.max_events > 64:
            raise ValueError("max_events must be between 4 and 64")
        if self.test_timeout_seconds <= 0 or self.test_timeout_seconds > 300:
            raise ValueError("test timeout must be between 0 and 300 seconds")
        if self.max_replacement_bytes < 1 or self.max_replacement_bytes > 1_048_576:
            raise ValueError("replacement byte limit must be between 1 and 1048576")

    @property
    def max_attempts(self) -> int:
        return self.max_replans + 1


@dataclass(frozen=True)
class FailureEvidence:
    """Bounded test failure evidence used as the next planning input."""

    source: str
    attempt_number: int
    status: str
    exit_code: int | None
    stdout_sha256: str
    stderr_sha256: str
    log_sha256: str
    fingerprint: str
    output_truncated: bool
    summary: str
    trusted_target: str | None
    diagnostic_log: str = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "log_sha256": self.log_sha256,
            "fingerprint": self.fingerprint,
            "output_truncated": self.output_truncated,
            "summary": _safe_text(self.summary, 1_000),
            "trusted_target": self.trusted_target,
        }


@dataclass(frozen=True)
class ReplanContext:
    task_id: str
    run_id: str
    attempt_number: int
    remaining_replans: int
    failure_evidence: FailureEvidence


@dataclass(frozen=True)
class RepairCandidate:
    """A planner output bound to one exact failure evidence fingerprint."""

    attempt_number: int
    evidence_sha256: str
    relative_path: str
    expected_original_sha256: str
    replacement_text: str = field(repr=False)
    reason: str = ""
    summary: str = ""

    def fingerprint(self, normalized_path: str) -> str:
        return _json_digest(
            {
                "attempt_number": self.attempt_number,
                "evidence_sha256": self.evidence_sha256.casefold(),
                "relative_path": normalized_path,
                "expected_original_sha256": self.expected_original_sha256.casefold(),
                "replacement_sha256": _sha256_text(self.replacement_text),
            }
        )


@dataclass(frozen=True)
class ApprovalInstruction:
    decision: str
    reviewer_id: str


CandidateProvider = Callable[[ReplanContext], RepairCandidate | None]
ApprovalProvider = Callable[[ReplanContext, dict[str, Any]], ApprovalInstruction | None]


class BoundedReplanningWorkflow:
    """Coordinate a bounded sequence of independently approved M4 attempts."""

    schema_version = 1

    def __init__(
        self,
        source_root: str | os.PathLike[str],
        *,
        report_path: str | os.PathLike[str],
        initial_failure_log: str,
        allowed_paths: tuple[str, ...] | list[str],
        task_id: str | None = None,
        run_id: str | None = None,
        limits: ReplanLimits | None = None,
        code_version: str = "unknown",
    ) -> None:
        if not isinstance(initial_failure_log, str) or not initial_failure_log.strip():
            raise ReplanError(
                "missing_failure_evidence", "initial failure evidence is required"
            )
        if not allowed_paths:
            raise ReplanError("missing_scope", "at least one allowed path is required")
        self.workspace = AuthorizedWorkspace(source_root)
        normalized_paths: set[str] = set()
        for value in allowed_paths:
            try:
                normalized_paths.add(
                    "/".join(AuthorizedWorkspace.normalize_relative_path(value))
                )
            except (RepoError, TypeError, ValueError) as exc:
                raise ReplanError("invalid_scope", "allowed path is invalid") from exc
        self.allowed_paths = tuple(sorted(normalized_paths))
        self.report_path = Path(report_path).expanduser().resolve()
        self.task_id = task_id or f"task-{uuid.uuid4()}"
        self.run_id = run_id or str(uuid.uuid4())
        self.limits = limits or ReplanLimits()
        self.code_version = code_version
        self.state = ReplanState.CREATED
        self.stop_reason: str | None = None
        self.started_at = _utc()
        self._started_monotonic = time.monotonic()
        self._last_event_monotonic = self._started_monotonic
        self.events: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.source_before = _workspace_snapshot(self.workspace)
        self.source_after: dict[str, Any] | None = None
        self._candidate_fingerprints: set[str] = set()
        self._failure_fingerprints: set[str] = set()
        self._current_evidence = self._initial_evidence(initial_failure_log)
        self._failure_fingerprints.add(self._current_evidence.fingerprint)
        self._event(ReplanState.CREATED, "workflow_created", {})

    @staticmethod
    def _initial_evidence(log: str) -> FailureEvidence:
        clean = _safe_text(log, 8_000)
        stdout_sha = _sha256_text(clean)
        stderr_sha = _sha256_text("")
        fingerprint = _json_digest(
            {
                "source": "initial",
                "status": "failed",
                "exit_code": None,
                "stdout_sha256": stdout_sha,
                "stderr_sha256": stderr_sha,
            }
        )
        return FailureEvidence(
            source="initial",
            attempt_number=0,
            status="failed",
            exit_code=None,
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
            log_sha256=_sha256_text(clean),
            fingerprint=fingerprint,
            output_truncated=len(clean) < len(log),
            summary=clean,
            trusted_target=None,
            diagnostic_log=clean,
        )

    @staticmethod
    def _post_failure_evidence(
        attempt_number: int,
        result: dict[str, Any] | None,
        *,
        trusted_target: str,
    ) -> FailureEvidence | None:
        if not isinstance(result, dict):
            return None
        stdout = _safe_text(result.get("stdout_tail", ""), 8_000)
        stderr = _safe_text(result.get("stderr_tail", ""), 8_000)
        error_message = _safe_text(result.get("error_message", ""), 1_000)
        observed_log = "\n".join(
            part for part in (stdout, stderr, error_message) if part.strip()
        ).strip()
        if not observed_log:
            return None
        diagnostic_log = (
            f"{observed_log}\n{trusted_target}:1: "
            "trusted prior target; current digest is revalidated before proposal"
        )
        status = str(result.get("status") or "unknown")
        exit_code = result.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            return None
        stdout_sha = _sha256_text(stdout)
        stderr_sha = _sha256_text(stderr)
        log_sha = _sha256_text(diagnostic_log)
        fingerprint = _json_digest(
            {
                "source": "post_apply",
                "status": status,
                "exit_code": exit_code,
                "stdout_signature_sha256": _sha256_text(
                    _failure_signature_text(stdout)
                ),
                "stderr_signature_sha256": _sha256_text(
                    _failure_signature_text(stderr)
                ),
                "error_type": str(result.get("error_type") or ""),
            }
        )
        return FailureEvidence(
            source="post_apply",
            attempt_number=attempt_number,
            status=status,
            exit_code=exit_code,
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
            log_sha256=log_sha,
            fingerprint=fingerprint,
            output_truncated=bool(result.get("output_truncated")),
            summary=observed_log[:1_000],
            trusted_target=trusted_target,
            diagnostic_log=diagnostic_log,
        )

    def _status(self) -> str:
        if self.state is ReplanState.COMPLETED:
            return "VERIFIED"
        if self.state in {
            ReplanState.APPROVAL_PENDING,
            ReplanState.APPROVAL_REQUIRED,
            ReplanState.APPROVAL_REJECTED,
            ReplanState.RETRY_EXHAUSTED,
            ReplanState.REPEATED_FAILURE,
            ReplanState.INSUFFICIENT_EVIDENCE,
            ReplanState.TEST_TIMEOUT,
        }:
            return "PARTIAL"
        return "NOT_VERIFIED"

    def _report(self) -> dict[str, Any]:
        source_after = self.source_after or _workspace_snapshot(self.workspace)
        return {
            "schema_version": self.schema_version,
            "kind": "devflow_bounded_replanning_report",
            "workflow": "bounded_evidence_replanning",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "code_version": _safe_text(self.code_version, 200),
            "workspace_id": self.workspace.workspace_id,
            "status": self._status(),
            "state": self.state.value,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "ended_at": _utc(),
            "limits": {
                **asdict(self.limits),
                "max_attempts": self.limits.max_attempts,
            },
            "allowed_paths": list(self.allowed_paths),
            "initial_failure": self._initial_evidence_dict(),
            "source_workspace": {
                "before": self.source_before,
                "after": source_after,
                "unchanged": source_after == self.source_before,
            },
            "attempts": self.attempts,
            "events": self.events,
        }

    def _initial_evidence_dict(self) -> dict[str, Any]:
        for attempt in self.attempts:
            value = attempt.get("input_evidence")
            if isinstance(value, dict) and value.get("source") == "initial":
                return value
        return self._current_evidence.to_dict()

    def _persist(self) -> None:
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.report_path.with_name(self.report_path.name + ".tmp")
            temporary.write_text(
                json.dumps(
                    self._report(), ensure_ascii=False, indent=2, sort_keys=True
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.report_path)
        except (OSError, TypeError, ValueError) as exc:
            raise ReplanError(
                "persistence_error", "M5 report could not be persisted"
            ) from exc

    def _event(self, state: ReplanState, phase: str, payload: dict[str, Any]) -> None:
        if self.events:
            previous = ReplanState(self.events[-1]["state"])
            if state not in _TRANSITIONS.get(previous, set()):
                raise ReplanError(
                    "invalid_state_transition",
                    f"cannot move {previous.value} to {state.value}",
                )
        sequence = len(self.events) + 1
        if sequence > self.limits.max_events:
            raise ReplanError("step_budget_exhausted", "M5 event budget exhausted")
        now = time.monotonic()
        self.state = state
        self.events.append(
            {
                "sequence": sequence,
                "event_id": str(uuid.uuid4()),
                "state": state.value,
                "phase": phase,
                "at": _utc(),
                "elapsed_seconds": round(now - self._started_monotonic, 3),
                "duration_seconds": round(now - self._last_event_monotonic, 3),
                "payload": _safe_value(payload),
            }
        )
        self._last_event_monotonic = now
        self._persist()

    def _stop(
        self, state: ReplanState, reason: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.stop_reason = reason
        try:
            self._event(state, reason, payload or {})
        except ReplanError:
            self.state = state
            self._persist()
        self.source_after = _workspace_snapshot(self.workspace)
        self._persist()
        return self._report()

    def _validate_candidate(
        self, candidate: RepairCandidate, context: ReplanContext
    ) -> tuple[str, str]:
        if candidate.attempt_number != context.attempt_number:
            raise ReplanError(
                "candidate_identity_mismatch", "candidate attempt number is stale"
            )
        if (
            not _HEX64_RE.fullmatch(candidate.evidence_sha256)
            or candidate.evidence_sha256.casefold()
            != context.failure_evidence.fingerprint.casefold()
        ):
            raise ReplanError(
                "candidate_evidence_mismatch",
                "candidate is not bound to the latest failure evidence",
            )
        if not _HEX64_RE.fullmatch(candidate.expected_original_sha256):
            raise ReplanError(
                "missing_original_digest", "candidate lacks a source digest"
            )
        if (
            not isinstance(candidate.replacement_text, str)
            or "\x00" in candidate.replacement_text
        ):
            raise ReplanError("invalid_replacement", "candidate replacement is invalid")
        if (
            len(candidate.replacement_text.encode("utf-8"))
            > self.limits.max_replacement_bytes
        ):
            raise ReplanError(
                "candidate_out_of_scope", "candidate replacement exceeds the byte limit"
            )
        if not candidate.reason.strip():
            raise ReplanError("missing_replan_reason", "candidate reason is required")
        try:
            normalized = "/".join(
                AuthorizedWorkspace.normalize_relative_path(candidate.relative_path)
            )
        except (RepoError, TypeError, ValueError) as exc:
            raise ReplanError(
                "invalid_candidate_path", "candidate path is invalid"
            ) from exc
        if normalized not in self.allowed_paths:
            raise ReplanError(
                "candidate_out_of_scope", "candidate path is outside the allowed scope"
            )
        fingerprint = candidate.fingerprint(normalized)
        if fingerprint in self._candidate_fingerprints:
            raise ReplanError(
                "duplicate_candidate", "candidate does not provide a new repair"
            )
        return normalized, fingerprint

    def _attempt_paths(self, attempt_number: int) -> tuple[Path, Path, str]:
        attempts_dir = self.report_path.parent / "attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.run_id}-attempt-{attempt_number:02d}"
        report = attempts_dir / f"{stem}.json"
        ledger = attempts_dir / f"{stem}.sqlite3"
        relative = report.relative_to(self.report_path.parent).as_posix()
        return report, ledger, relative

    @staticmethod
    def _attempt_summary(
        *,
        attempt_number: int,
        context: ReplanContext,
        candidate: RepairCandidate,
        normalized_path: str,
        candidate_fingerprint: str,
        m4_report: dict[str, Any],
        attempt_report_path: Path,
        attempt_report_relative: str,
        failure_evidence: FailureEvidence | None,
    ) -> dict[str, Any]:
        proposal = m4_report.get("proposal") or {}
        approval = m4_report.get("approval")
        tests = m4_report.get("tests") or {}
        return {
            "attempt_number": attempt_number,
            "attempt_run_id": m4_report.get("run_id"),
            "task_id": m4_report.get("task_id"),
            "input_evidence": context.failure_evidence.to_dict(),
            "candidate": {
                "fingerprint": candidate_fingerprint,
                "attempt_number": candidate.attempt_number,
                "evidence_sha256": candidate.evidence_sha256.casefold(),
                "reason": _safe_text(candidate.reason, 1_000),
                "summary": _safe_text(candidate.summary, 1_000),
                "relative_path": normalized_path,
                "expected_original_sha256": candidate.expected_original_sha256.casefold(),
                "replacement_sha256": _sha256_text(candidate.replacement_text),
            },
            "proposal_id": proposal.get("proposal_id"),
            "patch_sha256": proposal.get("patch_sha256"),
            "approval": _safe_value(approval),
            "m4_state": m4_report.get("state"),
            "m4_status": m4_report.get("status"),
            "m4_stop_reason": m4_report.get("stop_reason"),
            "tests": _safe_value(tests),
            "source_workspace": _safe_value(m4_report.get("source_workspace")),
            "receipt_count": len((m4_report.get("ledger") or {}).get("receipts", [])),
            "failure_evidence": failure_evidence.to_dict()
            if failure_evidence
            else None,
            "attempt_report": attempt_report_relative,
            "attempt_report_sha256": _sha256_bytes(attempt_report_path.read_bytes()),
        }

    def _map_m4_terminal(self, report: dict[str, Any]) -> tuple[ReplanState, str]:
        state = report.get("state")
        if state == WorkflowState.REJECTED.value:
            return ReplanState.APPROVAL_REJECTED, "approval_rejected"
        if state == WorkflowState.STALE_WORKSPACE_FILE.value:
            return ReplanState.SAFETY_REJECTED, "stale_workspace_file"
        if state == WorkflowState.PERSISTENCE_ERROR.value:
            return ReplanState.PERSISTENCE_ERROR, "persistence_error"
        if state == WorkflowState.RESULT_UNKNOWN.value:
            return ReplanState.RESULT_UNKNOWN, "result_unknown_do_not_replay"
        return ReplanState.FAILED, str(report.get("stop_reason") or "attempt_failed")

    def execute(
        self,
        *,
        candidate_provider: CandidateProvider,
        approval_provider: ApprovalProvider,
    ) -> dict[str, Any]:
        """Run at most ``max_replans + 1`` evidence-bound repair attempts."""
        if self.state is not ReplanState.CREATED:
            return self._report()
        try:
            for attempt_number in range(1, self.limits.max_attempts + 1):
                self._event(
                    ReplanState.PLANNING,
                    "planning_started",
                    {
                        "attempt_number": attempt_number,
                        "evidence_sha256": self._current_evidence.fingerprint,
                    },
                )
                context = ReplanContext(
                    task_id=self.task_id,
                    run_id=self.run_id,
                    attempt_number=attempt_number,
                    remaining_replans=self.limits.max_attempts - attempt_number,
                    failure_evidence=self._current_evidence,
                )
                candidate = candidate_provider(context)
                if candidate is None:
                    return self._stop(
                        ReplanState.INSUFFICIENT_EVIDENCE,
                        "planner_declined_without_candidate",
                        {"attempt_number": attempt_number},
                    )
                if not isinstance(candidate, RepairCandidate):
                    raise ReplanError(
                        "invalid_candidate", "planner returned an invalid candidate"
                    )
                normalized_path, candidate_fingerprint = self._validate_candidate(
                    candidate, context
                )
                self._candidate_fingerprints.add(candidate_fingerprint)
                (
                    attempt_report_path,
                    ledger_path,
                    attempt_report_relative,
                ) = self._attempt_paths(attempt_number)
                attempt_run_id = f"{self.run_id}-attempt-{attempt_number:02d}"
                workflow = RepairWorkflow(
                    self.workspace.root,
                    ledger_path=ledger_path,
                    report_path=attempt_report_path,
                    task_id=self.task_id,
                    run_id=attempt_run_id,
                    limits=WorkflowLimits(
                        max_steps=12,
                        max_retries=0,
                        test_timeout_seconds=self.limits.test_timeout_seconds,
                    ),
                    code_version=self.code_version,
                )
                preview = workflow.prepare(
                    relative_path=normalized_path,
                    replacement_text=candidate.replacement_text,
                    failure_log=self._current_evidence.diagnostic_log,
                    expected_original_sha256=candidate.expected_original_sha256,
                    summary=candidate.summary,
                )
                if preview.get("state") != WorkflowState.APPROVAL_PENDING.value:
                    attempt = self._attempt_summary(
                        attempt_number=attempt_number,
                        context=context,
                        candidate=candidate,
                        normalized_path=normalized_path,
                        candidate_fingerprint=candidate_fingerprint,
                        m4_report=preview,
                        attempt_report_path=attempt_report_path,
                        attempt_report_relative=attempt_report_relative,
                        failure_evidence=None,
                    )
                    self.attempts.append(attempt)
                    terminal, reason = self._map_m4_terminal(preview)
                    return self._stop(
                        terminal, reason, {"attempt_number": attempt_number}
                    )
                self._event(
                    ReplanState.APPROVAL_PENDING,
                    "approval_pending",
                    {
                        "attempt_number": attempt_number,
                        "proposal_id": (preview.get("proposal") or {}).get(
                            "proposal_id"
                        ),
                        "patch_sha256": (preview.get("proposal") or {}).get(
                            "patch_sha256"
                        ),
                    },
                )
                approval = approval_provider(context, preview)
                if approval is None:
                    attempt = self._attempt_summary(
                        attempt_number=attempt_number,
                        context=context,
                        candidate=candidate,
                        normalized_path=normalized_path,
                        candidate_fingerprint=candidate_fingerprint,
                        m4_report=preview,
                        attempt_report_path=attempt_report_path,
                        attempt_report_relative=attempt_report_relative,
                        failure_evidence=None,
                    )
                    self.attempts.append(attempt)
                    return self._stop(
                        ReplanState.APPROVAL_REQUIRED,
                        "explicit_approval_required",
                        {"attempt_number": attempt_number},
                    )
                if not isinstance(approval, ApprovalInstruction):
                    raise ReplanError(
                        "invalid_approval_instruction",
                        "reviewer returned an invalid approval instruction",
                    )
                decision = approval.decision.upper()
                if (
                    decision not in {"APPROVED", "REJECTED"}
                    or not approval.reviewer_id.strip()
                ):
                    return self._stop(
                        ReplanState.FAILED,
                        "invalid_approval_instruction",
                        {"attempt_number": attempt_number},
                    )
                if decision == "APPROVED":
                    self._event(
                        ReplanState.ATTEMPT_EXECUTING,
                        "approved_attempt_started",
                        {"attempt_number": attempt_number},
                    )
                finalized = workflow.finalize(
                    decision=decision, reviewer_id=approval.reviewer_id
                )
                failure_evidence = (
                    self._post_failure_evidence(
                        attempt_number,
                        workflow.post_apply,
                        trusted_target=normalized_path,
                    )
                    if finalized.get("state") == WorkflowState.TEST_FAILED.value
                    else None
                )
                attempt = self._attempt_summary(
                    attempt_number=attempt_number,
                    context=context,
                    candidate=candidate,
                    normalized_path=normalized_path,
                    candidate_fingerprint=candidate_fingerprint,
                    m4_report=finalized,
                    attempt_report_path=attempt_report_path,
                    attempt_report_relative=attempt_report_relative,
                    failure_evidence=failure_evidence,
                )
                self.attempts.append(attempt)
                if finalized.get("state") == WorkflowState.COMPLETED.value:
                    self.source_after = _workspace_snapshot(self.workspace)
                    if self.source_after != self.source_before:
                        return self._stop(
                            ReplanState.SAFETY_REJECTED,
                            "source_workspace_changed",
                            {"attempt_number": attempt_number},
                        )
                    self.stop_reason = "verification_passed"
                    self._event(
                        ReplanState.COMPLETED,
                        "verification_passed",
                        {"attempt_number": attempt_number},
                    )
                    self._persist()
                    return self._report()
                if finalized.get("state") == WorkflowState.REJECTED.value:
                    return self._stop(
                        ReplanState.APPROVAL_REJECTED,
                        "approval_rejected",
                        {"attempt_number": attempt_number},
                    )
                if finalized.get("state") != WorkflowState.TEST_FAILED.value:
                    terminal, reason = self._map_m4_terminal(finalized)
                    return self._stop(
                        terminal, reason, {"attempt_number": attempt_number}
                    )
                if finalized.get("stop_reason") != "verification_failed":
                    return self._stop(
                        ReplanState.SAFETY_REJECTED,
                        str(finalized.get("stop_reason") or "unsafe_attempt_failure"),
                        {"attempt_number": attempt_number},
                    )
                post = workflow.post_apply or {}
                if post.get("status") == "timeout":
                    return self._stop(
                        ReplanState.TEST_TIMEOUT,
                        "verification_timeout",
                        {"attempt_number": attempt_number},
                    )
                if failure_evidence is None:
                    return self._stop(
                        ReplanState.INSUFFICIENT_EVIDENCE,
                        "missing_verification_failure_evidence",
                        {"attempt_number": attempt_number},
                    )
                if failure_evidence.fingerprint in self._failure_fingerprints:
                    return self._stop(
                        ReplanState.REPEATED_FAILURE,
                        "repeated_failure_without_new_evidence",
                        {
                            "attempt_number": attempt_number,
                            "failure_sha256": failure_evidence.fingerprint,
                        },
                    )
                self._failure_fingerprints.add(failure_evidence.fingerprint)
                if attempt_number >= self.limits.max_attempts:
                    return self._stop(
                        ReplanState.RETRY_EXHAUSTED,
                        "replan_limit_reached",
                        {"attempt_number": attempt_number},
                    )
                self._current_evidence = failure_evidence
                self._event(
                    ReplanState.REPLAN_REQUIRED,
                    "new_failure_evidence_available",
                    {
                        "attempt_number": attempt_number,
                        "failure_sha256": failure_evidence.fingerprint,
                    },
                )
            return self._stop(ReplanState.RETRY_EXHAUSTED, "replan_limit_reached")
        except WorkflowError as exc:
            code = getattr(exc, "code", "replan_error")
            state = (
                ReplanState.PERSISTENCE_ERROR
                if code == "persistence_error"
                else ReplanState.SAFETY_REJECTED
                if code
                in {
                    "candidate_out_of_scope",
                    "invalid_candidate_path",
                    "missing_original_digest",
                    "candidate_evidence_mismatch",
                }
                else ReplanState.REPEATED_FAILURE
                if code == "duplicate_candidate"
                else ReplanState.FAILED
            )
            return self._stop(state, str(code), {"error_type": type(exc).__name__})
        except Exception as exc:
            return self._stop(
                ReplanState.FAILED,
                "planner_or_runtime_error",
                {"error_type": type(exc).__name__},
            )

    @staticmethod
    def replay_report(path: str | os.PathLike[str]) -> dict[str, Any]:
        """Validate an M5 report and each M4 attempt without side effects."""
        report_path = Path(path).expanduser().resolve()
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplanError("invalid_report", "M5 report could not be read") from exc
        if not isinstance(data, dict) or (
            data.get("kind") != "devflow_bounded_replanning_report"
            or data.get("schema_version") != 1
        ):
            raise ReplanError("invalid_report", "unsupported M5 report")
        for field_name in ("run_id", "task_id", "workspace_id"):
            if (
                not isinstance(data.get(field_name), str)
                or not data[field_name].strip()
            ):
                raise ReplanError("invalid_report", f"missing {field_name}")
        try:
            final_state = ReplanState(data.get("state"))
        except ValueError as exc:
            raise ReplanError("invalid_report", "unknown M5 state") from exc
        expected_status = (
            "VERIFIED"
            if final_state is ReplanState.COMPLETED
            else "PARTIAL"
            if final_state
            in {
                ReplanState.APPROVAL_PENDING,
                ReplanState.APPROVAL_REQUIRED,
                ReplanState.APPROVAL_REJECTED,
                ReplanState.RETRY_EXHAUSTED,
                ReplanState.REPEATED_FAILURE,
                ReplanState.INSUFFICIENT_EVIDENCE,
                ReplanState.TEST_TIMEOUT,
            }
            else "NOT_VERIFIED"
        )
        if data.get("status") != expected_status:
            raise ReplanError("invalid_report", "M5 status does not match state")
        events = data.get("events")
        if not isinstance(events, list) or not events:
            raise ReplanError("invalid_report", "M5 report has no events")
        for sequence, event in enumerate(events, 1):
            if not isinstance(event, dict) or (
                event.get("sequence") != sequence
                or not isinstance(event.get("event_id"), str)
                or not event.get("event_id")
                or not isinstance(event.get("payload"), dict)
            ):
                raise ReplanError("invalid_report", "M5 events are not sequential")
            try:
                event_state = ReplanState(event.get("state"))
            except ValueError as exc:
                raise ReplanError("invalid_report", "unknown M5 event state") from exc
            if sequence == 1:
                if event_state is not ReplanState.CREATED:
                    raise ReplanError("invalid_report", "M5 does not start in CREATED")
            else:
                previous = ReplanState(events[sequence - 2]["state"])
                if event_state not in _TRANSITIONS.get(previous, set()):
                    raise ReplanError("invalid_report", "invalid M5 transition")
        if events[-1].get("state") != final_state.value:
            raise ReplanError("invalid_report", "M5 final state does not match events")
        source = data.get("source_workspace")
        if not isinstance(source, dict) or not isinstance(
            source.get("unchanged"), bool
        ):
            raise ReplanError("invalid_report", "missing source workspace evidence")
        for snapshot_name in ("before", "after"):
            snapshot = source.get(snapshot_name)
            if not isinstance(snapshot, dict) or (
                not isinstance(snapshot.get("file_count"), int)
                or not _HEX64_RE.fullmatch(str(snapshot.get("sha256", "")))
            ):
                raise ReplanError("invalid_report", "invalid workspace snapshot")
        attempts = data.get("attempts")
        if not isinstance(attempts, list):
            raise ReplanError("invalid_report", "missing M5 attempts")
        limits = data.get("limits")
        if not isinstance(limits, dict) or (
            not isinstance(limits.get("max_replans"), int)
            or limits["max_replans"] < 0
            or limits["max_replans"] > 3
            or limits.get("max_attempts") != limits["max_replans"] + 1
            or len(attempts) > limits["max_attempts"]
        ):
            raise ReplanError("invalid_report", "invalid M5 attempt budget")
        initial_failure = data.get("initial_failure")
        if not isinstance(initial_failure, dict) or not _HEX64_RE.fullmatch(
            str(initial_failure.get("fingerprint", ""))
        ):
            raise ReplanError("invalid_report", "invalid initial failure evidence")
        previous_failure: str | None = None
        proposal_ids: set[str] = set()
        for expected_attempt, attempt in enumerate(attempts, 1):
            if (
                not isinstance(attempt, dict)
                or attempt.get("attempt_number") != expected_attempt
            ):
                raise ReplanError("invalid_report", "M5 attempts are not sequential")
            if attempt.get("task_id") != data["task_id"]:
                raise ReplanError("invalid_report", "attempt task identity mismatch")
            expected_run = f"{data['run_id']}-attempt-{expected_attempt:02d}"
            if attempt.get("attempt_run_id") != expected_run:
                raise ReplanError("invalid_report", "attempt run identity mismatch")
            input_evidence = attempt.get("input_evidence")
            candidate = attempt.get("candidate")
            if not isinstance(input_evidence, dict) or not isinstance(candidate, dict):
                raise ReplanError("invalid_report", "attempt evidence is incomplete")
            input_fingerprint = input_evidence.get("fingerprint")
            if not _HEX64_RE.fullmatch(str(input_fingerprint or "")):
                raise ReplanError("invalid_report", "invalid attempt evidence digest")
            if expected_attempt > 1 and input_fingerprint != previous_failure:
                raise ReplanError(
                    "invalid_report", "replan does not use latest evidence"
                )
            if (
                expected_attempt == 1
                and input_fingerprint != initial_failure["fingerprint"]
            ):
                raise ReplanError("invalid_report", "first attempt evidence mismatch")
            if (
                candidate.get("attempt_number") != expected_attempt
                or candidate.get("evidence_sha256") != input_fingerprint
            ):
                raise ReplanError(
                    "invalid_report", "candidate evidence binding mismatch"
                )
            if not _HEX64_RE.fullmatch(str(candidate.get("fingerprint", ""))):
                raise ReplanError("invalid_report", "invalid candidate digest")
            for digest_name in ("expected_original_sha256", "replacement_sha256"):
                if not _HEX64_RE.fullmatch(str(candidate.get(digest_name, ""))):
                    raise ReplanError(
                        "invalid_report", "invalid candidate source binding"
                    )
            try:
                normalized = "/".join(
                    AuthorizedWorkspace.normalize_relative_path(
                        candidate.get("relative_path")
                    )
                )
            except (RepoError, TypeError, ValueError) as exc:
                raise ReplanError("invalid_report", "invalid candidate path") from exc
            allowed_paths = data.get("allowed_paths")
            if (
                normalized != candidate.get("relative_path")
                or not isinstance(allowed_paths, list)
                or normalized not in allowed_paths
            ):
                raise ReplanError("invalid_report", "candidate path is outside scope")
            recomputed_candidate = _json_digest(
                {
                    "attempt_number": candidate["attempt_number"],
                    "evidence_sha256": candidate["evidence_sha256"],
                    "relative_path": normalized,
                    "expected_original_sha256": candidate["expected_original_sha256"],
                    "replacement_sha256": candidate["replacement_sha256"],
                }
            )
            if recomputed_candidate != candidate["fingerprint"]:
                raise ReplanError("invalid_report", "candidate digest mismatch")
            relative_value = attempt.get("attempt_report")
            if not isinstance(relative_value, str):
                raise ReplanError("invalid_report", "missing attempt report path")
            candidate_path = (report_path.parent / relative_value).resolve()
            try:
                candidate_path.relative_to(report_path.parent)
            except ValueError as exc:
                raise ReplanError(
                    "invalid_report", "attempt report escapes run root"
                ) from exc
            try:
                actual_sha = _sha256_bytes(candidate_path.read_bytes())
            except OSError as exc:
                raise ReplanError(
                    "invalid_report", "attempt report is unavailable"
                ) from exc
            if actual_sha != attempt.get("attempt_report_sha256"):
                raise ReplanError("invalid_report", "attempt report digest mismatch")
            replay = RepairWorkflow.replay_report(candidate_path)
            if (
                replay.get("run_id") != attempt.get("attempt_run_id")
                or replay.get("task_id") != data["task_id"]
                or replay.get("final_state") != attempt.get("m4_state")
                or replay.get("status") != attempt.get("m4_status")
            ):
                raise ReplanError("invalid_report", "M4 attempt summary mismatch")
            if attempt.get("m4_state") in {
                WorkflowState.COMPLETED.value,
                WorkflowState.TEST_FAILED.value,
            }:
                approval = attempt.get("approval")
                source_evidence = attempt.get("source_workspace")
                if (
                    not isinstance(approval, dict)
                    or approval.get("decision") != "APPROVED"
                    or attempt.get("receipt_count") != 1
                    or not isinstance(source_evidence, dict)
                    or source_evidence.get("unchanged") is not True
                ):
                    raise ReplanError(
                        "invalid_report",
                        "attempt bypassed approval or source isolation",
                    )
                tests = attempt.get("tests")
                post_apply = (
                    tests.get("post_apply") if isinstance(tests, dict) else None
                )
                if not isinstance(post_apply, dict):
                    raise ReplanError("invalid_report", "attempt lacks test evidence")
                if attempt.get("m4_state") == WorkflowState.TEST_FAILED.value and (
                    post_apply.get("status") not in {"failed", "timeout"}
                    or post_apply.get("exit_code") == 0
                ):
                    raise ReplanError(
                        "invalid_report", "failed attempt test is inconsistent"
                    )
            proposal_id = attempt.get("proposal_id")
            if proposal_id:
                if proposal_id in proposal_ids:
                    raise ReplanError("invalid_report", "proposal identity was reused")
                proposal_ids.add(proposal_id)
            failure = attempt.get("failure_evidence")
            previous_failure = (
                failure.get("fingerprint") if isinstance(failure, dict) else None
            )
            if previous_failure is not None and not _HEX64_RE.fullmatch(
                str(previous_failure)
            ):
                raise ReplanError("invalid_report", "invalid failure evidence digest")
        if final_state is ReplanState.COMPLETED:
            if (
                not attempts
                or attempts[-1].get("m4_state") != WorkflowState.COMPLETED.value
            ):
                raise ReplanError(
                    "invalid_report", "verified M5 run lacks a passing attempt"
                )
            if source.get("unchanged") is not True or source.get(
                "before"
            ) != source.get("after"):
                raise ReplanError("invalid_report", "verified M5 run changed source")
            if any(
                attempt.get("source_workspace", {}).get("unchanged") is not True
                for attempt in attempts
            ):
                raise ReplanError("invalid_report", "attempt changed source workspace")
        return {
            "replayed": True,
            "write_replayed": False,
            "tests_replayed": False,
            "run_id": data["run_id"],
            "task_id": data["task_id"],
            "final_state": final_state.value,
            "status": data["status"],
            "attempt_count": len(attempts),
            "event_count": len(events),
        }
