"""Evidence-driven, replayable repair workflow for synthetic workspaces.

The workflow is intentionally a control-plane coordinator.  It composes the
existing read-only diagnostic, patch proposal, approval ledger, and isolated
pytest primitives without registering a new write-capable Agent tool.  The
source workspace is never modified; an approved proposal is applied only to a
fresh isolated copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .diagnostics import DiagnosticReport, diagnose_failures
from .patch_ledger import PatchLedger, PatchLedgerError, run_isolated_pytest
from .patches import PatchError, PatchProposal, create_patch_proposal
from .repository import AuthorizedWorkspace, RepoError, build_index


class WorkflowState(str, Enum):
    CREATED = "CREATED"
    BASELINE_TESTED = "BASELINE_TESTED"
    DIAGNOSED = "DIAGNOSED"
    PATCH_PROPOSED = "PATCH_PROPOSED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    TEST_FAILED = "TEST_FAILED"
    FAILED = "FAILED"
    STALE_WORKSPACE_FILE = "STALE_WORKSPACE_FILE"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"


_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.CREATED: {
        WorkflowState.BASELINE_TESTED,
        WorkflowState.FAILED,
        WorkflowState.PERSISTENCE_ERROR,
    },
    WorkflowState.BASELINE_TESTED: {
        WorkflowState.DIAGNOSED,
        WorkflowState.FAILED,
        WorkflowState.PERSISTENCE_ERROR,
    },
    WorkflowState.DIAGNOSED: {
        WorkflowState.PATCH_PROPOSED,
        WorkflowState.FAILED,
        WorkflowState.STALE_WORKSPACE_FILE,
        WorkflowState.PERSISTENCE_ERROR,
    },
    WorkflowState.PATCH_PROPOSED: {
        WorkflowState.APPROVAL_PENDING,
        WorkflowState.FAILED,
        WorkflowState.PERSISTENCE_ERROR,
    },
    WorkflowState.APPROVAL_PENDING: {
        WorkflowState.APPROVED,
        WorkflowState.REJECTED,
        WorkflowState.FAILED,
        WorkflowState.PERSISTENCE_ERROR,
    },
    WorkflowState.APPROVED: {
        WorkflowState.APPLIED,
        WorkflowState.STALE_WORKSPACE_FILE,
        WorkflowState.PERSISTENCE_ERROR,
        WorkflowState.RESULT_UNKNOWN,
        WorkflowState.FAILED,
    },
    WorkflowState.APPLIED: {WorkflowState.VERIFYING, WorkflowState.FAILED},
    WorkflowState.VERIFYING: {WorkflowState.COMPLETED, WorkflowState.TEST_FAILED},
}

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s\"']+[\\/])+[^\s\"']*")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_TERMINAL_STATES = {
    WorkflowState.COMPLETED,
    WorkflowState.REJECTED,
    WorkflowState.TEST_FAILED,
    WorkflowState.FAILED,
    WorkflowState.STALE_WORKSPACE_FILE,
    WorkflowState.PERSISTENCE_ERROR,
    WorkflowState.RESULT_UNKNOWN,
}


@dataclass(frozen=True)
class WorkflowLimits:
    """Hard bounds for one workflow run."""

    max_steps: int = 12
    max_retries: int = 1
    test_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_steps > 32:
            raise ValueError("max_steps must be between 1 and 32")
        if self.max_retries < 0 or self.max_retries > 3:
            raise ValueError("max_retries must be between 0 and 3")
        if self.test_timeout_seconds <= 0 or self.test_timeout_seconds > 300:
            raise ValueError("test timeout must be between 0 and 300 seconds")


class WorkflowError(PatchError):
    """Stable fail-closed workflow error."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def _workspace_snapshot(workspace: AuthorizedWorkspace) -> dict[str, Any]:
    index = build_index(workspace)
    records = sorted(index.file_records, key=lambda item: item.relative_path)
    digest_input = "\n".join(
        f"{item.relative_path}\0{item.size_bytes}\0{item.sha256}" for item in records
    )
    return {
        "file_count": len(records),
        "sha256": _sha256_text(digest_input),
    }


def _sanitize_test(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    allowed = {
        "test_run_id",
        "command",
        "status",
        "exit_code",
        "output_truncated",
        "elapsed_seconds",
        "started_at",
        "ended_at",
        "pid",
        "cleanup",
        "error_type",
        "error_message",
    }
    result = {key: value for key, value in result.items() if key in allowed}
    if "command" in result:
        result["command"] = ["pytest", "-q"]
    result["cleanup"] = _safe_value(result.get("cleanup") or {})
    if result.get("error_message"):
        result["error_message"] = _safe_text(result["error_message"], 500)
    return _safe_value(result)


class RepairWorkflow:
    """Coordinate one bounded diagnostic, approval, isolated apply, and test."""

    schema_version = 1

    def __init__(
        self,
        source_root: str | os.PathLike[str],
        *,
        ledger_path: str | os.PathLike[str],
        report_path: str | os.PathLike[str],
        task_id: str | None = None,
        run_id: str | None = None,
        limits: WorkflowLimits | None = None,
        code_version: str = "unknown",
    ) -> None:
        self.workspace = AuthorizedWorkspace(source_root)
        self.ledger = PatchLedger(ledger_path)
        self.report_path = Path(report_path).expanduser().resolve()
        self.task_id = task_id or f"task-{uuid.uuid4()}"
        self.run_id = run_id or str(uuid.uuid4())
        self.limits = limits or WorkflowLimits()
        self.code_version = code_version
        self.state = WorkflowState.CREATED
        self.started_at = _utc()
        self.steps = 0
        self.events: list[dict[str, Any]] = []
        self._started_monotonic = time.monotonic()
        self._last_event_monotonic = self._started_monotonic
        self.baseline: dict[str, Any] | None = None
        self.post_apply: dict[str, Any] | None = None
        self.diagnostic: DiagnosticReport | None = None
        self.proposal: PatchProposal | None = None
        self.approval: dict[str, Any] | None = None
        self.failure_log_sha256: str | None = None
        self.failure_log_size: int | None = None
        self.source_before = _workspace_snapshot(self.workspace)
        self.source_after: dict[str, Any] | None = None
        self.isolated_cleanup: bool | None = None
        self.stop_reason: str | None = None
        self._record_event(WorkflowState.CREATED, "workflow_created", {})

    def _report(self) -> dict[str, Any]:
        source_after = self.source_after or self.source_before
        if self.state == WorkflowState.COMPLETED:
            status = "VERIFIED"
        elif self.state in {
            WorkflowState.APPROVAL_PENDING,
            WorkflowState.REJECTED,
            WorkflowState.TEST_FAILED,
        }:
            status = "PARTIAL"
        else:
            status = "NOT_VERIFIED"
        proposal = self.proposal.to_dict() if self.proposal else None
        try:
            ledger = self.ledger.snapshot()
        except PatchLedgerError:
            ledger = {"error": "persistence_error"}
        sanitized_ledger = _safe_value(ledger)
        for receipt in (
            sanitized_ledger.get("receipts", [])
            if isinstance(sanitized_ledger, dict)
            else []
        ):
            receipt["destination_root"] = "<isolated>"
        return {
            "schema_version": self.schema_version,
            "kind": "devflow_repair_workflow_report",
            "workflow": "evidence_driven_repair",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "code_version": _safe_text(self.code_version, 200),
            "workspace_id": self.workspace.workspace_id,
            "status": status,
            "state": self.state.value,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "ended_at": _utc(),
            "input_failure_log_sha256": self.failure_log_sha256,
            "input_failure_log_size": self.failure_log_size,
            "limits": asdict(self.limits),
            "source_workspace": {
                "before": self.source_before,
                "after": source_after,
                "unchanged": source_after == self.source_before,
            },
            "isolated_copy_cleanup": self.isolated_cleanup,
            "diagnostic": self.diagnostic.to_compact_dict()
            if self.diagnostic
            else None,
            "proposal": _safe_value(proposal),
            "approval": _safe_value(self.approval),
            "tests": {
                "baseline": _sanitize_test(self.baseline),
                "post_apply": _sanitize_test(self.post_apply),
            },
            "ledger": sanitized_ledger,
            "events": self.events,
        }

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
            raise WorkflowError(
                "persistence_error", "workflow report could not be persisted"
            ) from exc

    def _record_event(
        self, state: WorkflowState, phase: str, payload: dict[str, Any]
    ) -> None:
        if self.events:
            current = WorkflowState(self.events[-1]["state"])
            if state not in _TRANSITIONS.get(current, set()):
                raise WorkflowError(
                    "invalid_state_transition",
                    f"cannot move {current.value} to {state.value}",
                )
        now = time.monotonic()
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise WorkflowError(
                "step_budget_exhausted", "workflow step budget exhausted"
            )
        self.state = state
        self.events.append(
            {
                "sequence": self.steps,
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
        self, state: WorkflowState, reason: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.stop_reason = reason
        try:
            self._record_event(state, reason, payload or {})
        except WorkflowError:
            self.state = state
            self._persist()
        return self._report()

    def _record_test(self, result: dict[str, Any], phase: str) -> None:
        proposal_id = self.proposal.proposal_id if self.proposal else None
        self.ledger.record_test_result(result, phase=phase, proposal_id=proposal_id)

    @staticmethod
    def _cleanup_isolated(path: Path) -> None:
        """Best-effort cleanup kept behind a seam so failure is observable."""
        shutil.rmtree(path, ignore_errors=True)

    def _assert_workspace_copy_safe(self) -> None:
        """Reject links before any runner or copy operation can follow them."""
        try:
            for current, directories, filenames in os.walk(
                self.workspace.root, followlinks=False
            ):
                for name in (*directories, *filenames):
                    candidate = Path(current) / name
                    is_junction = getattr(candidate, "is_junction", lambda: False)
                    if candidate.is_symlink() or is_junction():
                        relative = candidate.relative_to(self.workspace.root).as_posix()
                        raise WorkflowError(
                            "unsafe_workspace_link",
                            "workspace links are not allowed for isolated execution",
                            details={"relative_path": relative},
                        )
        except WorkflowError:
            raise
        except OSError as exc:
            raise WorkflowError(
                "workspace_scan_failed", "workspace links could not be checked"
            ) from exc

    def prepare(
        self,
        *,
        relative_path: str,
        replacement_text: str,
        failure_log: str | None = None,
        expected_original_sha256: str | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        """Run the read-only half and stop before approval."""
        if self.state != WorkflowState.CREATED:
            if self.state in _TERMINAL_STATES:
                return self._report()
            return self._stop(WorkflowState.FAILED, "workflow_already_started")
        try:
            self._assert_workspace_copy_safe()
            baseline = run_isolated_pytest(
                self.workspace.root,
                timeout_seconds=self.limits.test_timeout_seconds,
            )
            self.baseline = baseline
            self._record_test(baseline, "baseline")
            if baseline.get("status") != "failed" or baseline.get("exit_code") == 0:
                return self._stop(
                    WorkflowState.FAILED,
                    "baseline_not_failed",
                    {
                        "status": baseline.get("status"),
                        "exit_code": baseline.get("exit_code"),
                    },
                )
            self._record_event(
                WorkflowState.BASELINE_TESTED,
                "baseline_tested",
                {"exit_code": baseline.get("exit_code")},
            )
            log = failure_log
            if log is None:
                log = f"{baseline.get('stdout_tail', '')}\n{baseline.get('stderr_tail', '')}"
            self.failure_log_sha256 = _sha256_text(log)
            self.failure_log_size = len(log.encode("utf-8"))
            self.diagnostic = diagnose_failures(
                self.workspace, log, exit_code=baseline.get("exit_code")
            )
            self._record_event(
                WorkflowState.DIAGNOSED,
                "diagnosed",
                {
                    "status": self.diagnostic.status,
                    "evidence": self.diagnostic.metrics.get(
                        "trusted_evidence_items", 0
                    ),
                },
            )
            trusted = [
                item
                for failure in self.diagnostic.parsed_failures
                for item in failure.get("evidence", [])
                if item.get("relative_path") == relative_path.replace("\\", "/")
            ]
            if not trusted:
                return self._stop(
                    WorkflowState.FAILED,
                    "evidence_does_not_bind_target",
                    {"relative_path": relative_path},
                )
            current = self.workspace.read_text(relative_path, start_line=1)
            if (
                expected_original_sha256
                and current.full_file_sha256.casefold()
                != expected_original_sha256.casefold()
            ):
                return self._stop(
                    WorkflowState.STALE_WORKSPACE_FILE,
                    "expected_original_digest_mismatch",
                    {"relative_path": relative_path},
                )
            self.proposal = create_patch_proposal(
                self.workspace,
                relative_path,
                replacement_text,
                expected_original_sha256=current.full_file_sha256,
                task_id=self.task_id,
                run_id=self.run_id,
                summary=summary,
            )
            self.ledger.record_proposal(self.proposal)
            self._record_event(
                WorkflowState.PATCH_PROPOSED,
                "patch_proposed",
                {
                    "proposal_id": self.proposal.proposal_id,
                    "patch_sha256": self.proposal.patch_sha256,
                },
            )
            self._record_event(
                WorkflowState.APPROVAL_PENDING,
                "approval_pending",
                {"proposal_id": self.proposal.proposal_id},
            )
            return self._report()
        except (RepoError, PatchError, PatchLedgerError, WorkflowError) as exc:
            code = getattr(exc, "code", "workflow_error")
            state = (
                WorkflowState.STALE_WORKSPACE_FILE
                if code == "stale_workspace_file"
                else WorkflowState.PERSISTENCE_ERROR
                if code == "persistence_error"
                else WorkflowState.FAILED
            )
            return self._stop(state, str(code), {"error_type": type(exc).__name__})

    def finalize(
        self, *, decision: str = "NONE", reviewer_id: str | None = None
    ) -> dict[str, Any]:
        """Apply a decision; only APPROVED reaches the isolated copy."""
        if self.state != WorkflowState.APPROVAL_PENDING or self.proposal is None:
            if self.state in _TERMINAL_STATES:
                return self._report()
            return self._stop(WorkflowState.FAILED, "approval_not_pending")
        normalized = decision.upper()
        if normalized == "NONE":
            return self._report()
        if normalized not in {"APPROVED", "REJECTED"}:
            return self._stop(WorkflowState.FAILED, "invalid_approval_decision")
        isolated_parent: Path | None = None
        try:
            if not reviewer_id:
                return self._stop(WorkflowState.FAILED, "reviewer_required")
            if normalized == "REJECTED":
                decision_record = self.ledger.reject(
                    self.proposal, reviewer_id=reviewer_id
                )
                self.approval = decision_record.to_dict()
                return self._stop(
                    WorkflowState.REJECTED,
                    "approval_rejected",
                    {"proposal_id": self.proposal.proposal_id},
                )
            decision_record = self.ledger.approve(
                self.proposal, reviewer_id=reviewer_id
            )
            self.approval = decision_record.to_dict()
            self._record_event(
                WorkflowState.APPROVED,
                "approval_recorded",
                {"proposal_id": self.proposal.proposal_id},
            )
            self._assert_workspace_copy_safe()
            isolated_parent = Path(tempfile.mkdtemp(prefix="devflow-m4-isolated-"))
            isolated_root = isolated_parent / "repo"
            shutil.copytree(self.workspace.root, isolated_root)
            receipt = self.ledger.apply_approved(
                self.proposal,
                decision_record,
                source_workspace=self.workspace,
                destination_root=isolated_root,
            )
            self._record_event(
                WorkflowState.APPLIED,
                "patch_applied_to_isolated_copy",
                {
                    "proposal_id": receipt.proposal_id,
                    "patch_sha256": receipt.patch_sha256,
                },
            )
            self._record_event(WorkflowState.VERIFYING, "verification_started", {})
            self.post_apply = run_isolated_pytest(
                isolated_root, timeout_seconds=self.limits.test_timeout_seconds
            )
            self._record_test(self.post_apply, "post_apply")
            self._cleanup_isolated(isolated_parent)
            self.isolated_cleanup = not isolated_parent.exists()
            after = _workspace_snapshot(self.workspace)
            self.source_after = after
            if (
                self.post_apply.get("status") == "passed"
                and self.post_apply.get("exit_code") == 0
                and after == self.source_before
                and self.isolated_cleanup is True
            ):
                self.stop_reason = "verification_passed"
                self._record_event(
                    WorkflowState.COMPLETED,
                    "verification_passed",
                    {"exit_code": self.post_apply.get("exit_code")},
                )
            elif not self.isolated_cleanup:
                self._stop(
                    WorkflowState.TEST_FAILED,
                    "isolated_copy_cleanup_failed",
                    {
                        "status": self.post_apply.get("status"),
                        "exit_code": self.post_apply.get("exit_code"),
                    },
                )
            else:
                self._stop(
                    WorkflowState.TEST_FAILED,
                    "verification_failed",
                    {
                        "status": self.post_apply.get("status"),
                        "exit_code": self.post_apply.get("exit_code"),
                    },
                )
            return self._report()
        except (PatchLedgerError, PatchError, RepoError, OSError, WorkflowError) as exc:
            if isolated_parent is not None:
                self._cleanup_isolated(isolated_parent)
                self.isolated_cleanup = not isolated_parent.exists()
            code = getattr(exc, "code", "workflow_error")
            state = {
                "stale_workspace_file": WorkflowState.STALE_WORKSPACE_FILE,
                "result_unknown": WorkflowState.RESULT_UNKNOWN,
                "persistence_error": WorkflowState.PERSISTENCE_ERROR,
            }.get(code, WorkflowState.FAILED)
            return self._stop(state, str(code), {"error_type": type(exc).__name__})

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        decision = kwargs.pop("decision", "NONE")
        reviewer_id = kwargs.pop("reviewer_id", None)
        report = self.prepare(**kwargs)
        if report.get("state") != WorkflowState.APPROVAL_PENDING.value:
            return report
        return self.finalize(decision=decision, reviewer_id=reviewer_id)

    @staticmethod
    def replay_report(path: str | os.PathLike[str]) -> dict[str, Any]:
        """Validate a saved run without opening a workspace or applying a patch."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                "invalid_report", "workflow report could not be read"
            ) from exc
        if not isinstance(data, dict) or (
            data.get("kind") != "devflow_repair_workflow_report"
            or data.get("schema_version") != 1
        ):
            raise WorkflowError("invalid_report", "unsupported workflow report")
        for field in ("run_id", "task_id", "workspace_id"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                raise WorkflowError("invalid_report", f"missing {field}")
        events = data.get("events")
        if not isinstance(events, list) or not events:
            raise WorkflowError("invalid_report", "workflow report has no events")
        for expected, event in enumerate(events, 1):
            if not isinstance(event, dict):
                raise WorkflowError("invalid_report", "workflow event is not an object")
            if (
                event.get("sequence") != expected
                or not isinstance(event.get("state"), str)
                or not event.get("state")
                or not isinstance(event.get("event_id"), str)
                or not event.get("event_id")
                or not isinstance(event.get("phase"), str)
                or not isinstance(event.get("payload"), dict)
            ):
                raise WorkflowError(
                    "invalid_report", "workflow events are not sequential"
                )
            try:
                WorkflowState(event["state"])
            except ValueError as exc:
                raise WorkflowError("invalid_report", "unknown workflow state") from exc
            if expected > 1:
                previous = WorkflowState(events[expected - 2]["state"])
                current = WorkflowState(event["state"])
                if current not in _TRANSITIONS.get(previous, set()):
                    raise WorkflowError(
                        "invalid_report", "workflow event transition is invalid"
                    )
        if events[0]["state"] != WorkflowState.CREATED.value:
            raise WorkflowError("invalid_report", "workflow does not start in CREATED")
        final_state = WorkflowState(events[-1]["state"])
        if data.get("state") != final_state.value:
            raise WorkflowError("invalid_report", "report state does not match events")
        expected_status = (
            "VERIFIED"
            if final_state is WorkflowState.COMPLETED
            else "PARTIAL"
            if final_state
            in {
                WorkflowState.APPROVAL_PENDING,
                WorkflowState.REJECTED,
                WorkflowState.TEST_FAILED,
            }
            else "NOT_VERIFIED"
        )
        if data.get("status") != expected_status:
            raise WorkflowError("invalid_report", "report status does not match state")

        def require_digest(value: Any, label: str) -> str:
            if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
                raise WorkflowError("invalid_report", f"invalid {label}")
            return value.casefold()

        def validate_snapshot(value: Any, label: str) -> None:
            if not isinstance(value, dict):
                raise WorkflowError("invalid_report", f"missing {label} snapshot")
            if not isinstance(value.get("file_count"), int) or value["file_count"] < 0:
                raise WorkflowError("invalid_report", f"invalid {label} file count")
            require_digest(value.get("sha256"), f"{label} digest")

        source = data.get("source_workspace")
        if not isinstance(source, dict):
            raise WorkflowError("invalid_report", "missing source workspace evidence")
        validate_snapshot(source.get("before"), "source before")
        validate_snapshot(source.get("after"), "source after")
        if not isinstance(source.get("unchanged"), bool):
            raise WorkflowError("invalid_report", "invalid source unchanged flag")
        if data.get("input_failure_log_sha256") is not None:
            require_digest(data.get("input_failure_log_sha256"), "failure log digest")

        proposal = data.get("proposal")
        ledger = data.get("ledger")
        if (
            final_state
            in {
                WorkflowState.APPROVAL_PENDING,
                WorkflowState.REJECTED,
                WorkflowState.TEST_FAILED,
                WorkflowState.COMPLETED,
            }
            and proposal is None
        ):
            raise WorkflowError(
                "invalid_report", "terminal workflow lacks proposal evidence"
            )
        if proposal is not None:
            if not isinstance(proposal, dict) or not isinstance(ledger, dict):
                raise WorkflowError("invalid_report", "incomplete proposal ledger")
            for field in (
                "proposal_id",
                "task_id",
                "run_id",
                "workspace_id",
                "relative_path",
            ):
                if (
                    not isinstance(proposal.get(field), str)
                    or not proposal[field].strip()
                ):
                    raise WorkflowError("invalid_report", f"missing proposal {field}")
            if (
                proposal["task_id"] != data["task_id"]
                or proposal["run_id"] != data["run_id"]
            ):
                raise WorkflowError(
                    "invalid_report", "proposal identity does not match report"
                )
            if proposal["workspace_id"] != data["workspace_id"]:
                raise WorkflowError(
                    "invalid_report", "proposal workspace does not match report"
                )
            try:
                normalized_path = "/".join(
                    AuthorizedWorkspace.normalize_relative_path(
                        proposal["relative_path"]
                    )
                )
            except (RepoError, ValueError, TypeError) as exc:
                raise WorkflowError("invalid_report", "invalid proposal path") from exc
            if normalized_path != proposal["relative_path"]:
                raise WorkflowError("invalid_report", "proposal path is not normalized")
            for field in ("original_sha256", "replacement_sha256", "patch_sha256"):
                require_digest(proposal.get(field), f"proposal {field}")
            proposals = ledger.get("proposals")
            if not isinstance(proposals, list):
                raise WorkflowError("invalid_report", "missing ledger proposals")
            matches = [
                row
                for row in proposals
                if isinstance(row, dict)
                and row.get("proposal_id") == proposal["proposal_id"]
            ]
            if len(matches) != 1:
                raise WorkflowError(
                    "invalid_report", "proposal is not uniquely bound to ledger"
                )
            ledger_proposal = matches[0]
            for field in (
                "task_id",
                "run_id",
                "workspace_id",
                "relative_path",
                "original_sha256",
                "replacement_sha256",
                "patch_sha256",
                "status",
                "consumed",
            ):
                if ledger_proposal.get(field) != proposal.get(field):
                    raise WorkflowError(
                        "invalid_report", f"ledger proposal mismatch: {field}"
                    )

            approval = data.get("approval")
            decisions = ledger.get("decisions")
            receipts = ledger.get("receipts")
            if not isinstance(decisions, list) or not isinstance(receipts, list):
                raise WorkflowError("invalid_report", "incomplete ledger decisions")
            if approval is not None:
                if (
                    not isinstance(approval, dict)
                    or approval.get("proposal_id") != proposal["proposal_id"]
                ):
                    raise WorkflowError(
                        "invalid_report", "approval is not bound to proposal"
                    )
                if approval.get("patch_sha256") != proposal["patch_sha256"]:
                    raise WorkflowError(
                        "invalid_report", "approval patch digest mismatch"
                    )
                if approval.get("decision") not in {
                    "APPROVED",
                    "REJECTED",
                } or not approval.get("reviewer_id"):
                    raise WorkflowError("invalid_report", "invalid approval decision")
                matching_decisions = [
                    row
                    for row in decisions
                    if isinstance(row, dict)
                    and row.get("proposal_id") == proposal["proposal_id"]
                    and row.get("decision") == approval["decision"]
                    and row.get("patch_sha256") == proposal["patch_sha256"]
                ]
                if len(matching_decisions) != 1:
                    raise WorkflowError(
                        "invalid_report", "approval is not uniquely bound to ledger"
                    )
            if final_state is WorkflowState.APPROVAL_PENDING:
                if approval is not None or receipts:
                    raise WorkflowError(
                        "invalid_report", "pending report has approval or receipt"
                    )
            elif final_state is WorkflowState.REJECTED:
                if (
                    not isinstance(approval, dict)
                    or approval.get("decision") != "REJECTED"
                    or receipts
                ):
                    raise WorkflowError(
                        "invalid_report",
                        "rejected report has inconsistent side effects",
                    )

        if final_state is WorkflowState.COMPLETED:
            if data.get("isolated_copy_cleanup") is not True:
                raise WorkflowError(
                    "invalid_report", "verified report lacks cleanup proof"
                )
            if source.get("unchanged") is not True or source.get(
                "before"
            ) != source.get("after"):
                raise WorkflowError(
                    "invalid_report", "verified report has changed source workspace"
                )
            if not isinstance(proposal, dict) or not isinstance(
                data.get("approval"), dict
            ):
                raise WorkflowError(
                    "invalid_report", "verified report lacks proposal or approval"
                )
            if (
                proposal.get("status") != "APPLIED"
                or proposal.get("consumed") is not True
            ):
                raise WorkflowError(
                    "invalid_report", "verified report proposal is not consumed"
                )
            approval = data["approval"]
            if approval.get("decision") != "APPROVED":
                raise WorkflowError("invalid_report", "verified report lacks approval")
            tests = data.get("tests")
            if not isinstance(tests, dict):
                raise WorkflowError(
                    "invalid_report", "verified report lacks test evidence"
                )
            baseline = tests.get("baseline")
            post_apply = tests.get("post_apply")
            for item, label in ((baseline, "baseline"), (post_apply, "post_apply")):
                if not isinstance(item, dict) or not isinstance(
                    item.get("test_run_id"), str
                ):
                    raise WorkflowError(
                        "invalid_report", f"verified report lacks {label} test"
                    )
                cleanup = item.get("cleanup")
                if (
                    not isinstance(cleanup, dict)
                    or cleanup.get("remaining_parent") is not False
                ):
                    raise WorkflowError(
                        "invalid_report", f"{label} process cleanup is unverified"
                    )
            if baseline.get("status") != "failed" or baseline.get("exit_code") == 0:
                raise WorkflowError(
                    "invalid_report", "baseline test evidence is inconsistent"
                )
            if post_apply.get("status") != "passed" or post_apply.get("exit_code") != 0:
                raise WorkflowError(
                    "invalid_report", "post-apply test evidence is inconsistent"
                )
            diagnostic = data.get("diagnostic")
            if not isinstance(diagnostic, dict) or diagnostic.get("status") != "PASS":
                raise WorkflowError(
                    "invalid_report", "verified report lacks diagnostic evidence"
                )
            failures = diagnostic.get("failures")
            if not isinstance(failures, list) or not failures:
                raise WorkflowError(
                    "invalid_report", "verified report lacks trusted evidence"
                )
            evidence_items = [
                item
                for failure in failures
                if isinstance(failure, dict)
                for item in (failure.get("evidence") or [])
                if isinstance(item, dict)
            ]
            if not evidence_items:
                raise WorkflowError(
                    "invalid_report", "verified report lacks trusted evidence"
                )
            for evidence in evidence_items:
                path_value = evidence.get("relative_path")
                if not isinstance(path_value, str) or not path_value:
                    raise WorkflowError("invalid_report", "invalid evidence path")
                try:
                    normalized_evidence_path = "/".join(
                        AuthorizedWorkspace.normalize_relative_path(path_value)
                    )
                    if normalized_evidence_path != path_value:
                        raise ValueError("evidence path is not normalized")
                    start_line = evidence.get("start_line")
                    end_line = evidence.get("end_line")
                    if (
                        not isinstance(start_line, int)
                        or not isinstance(end_line, int)
                        or start_line < 1
                        or end_line < start_line
                    ):
                        raise ValueError("invalid evidence range")
                except (RepoError, ValueError, TypeError) as exc:
                    raise WorkflowError(
                        "invalid_report", "invalid evidence range or path"
                    ) from exc
                require_digest(evidence.get("full_file_sha256"), "evidence file digest")
            receipts = ledger.get("receipts") if isinstance(ledger, dict) else None
            if not isinstance(receipts, list) or len(receipts) != 1:
                raise WorkflowError(
                    "invalid_report", "verified report must have one receipt"
                )
            receipt = receipts[0]
            expected_receipt = {
                "proposal_id": proposal["proposal_id"],
                "relative_path": proposal["relative_path"],
                "before_sha256": proposal["original_sha256"],
                "after_sha256": proposal["replacement_sha256"],
                "patch_sha256": proposal["patch_sha256"],
                "write_count": 1,
            }
            if not isinstance(receipt, dict) or any(
                receipt.get(field) != expected
                for field, expected in expected_receipt.items()
            ):
                raise WorkflowError(
                    "invalid_report", "receipt does not bind exact proposal"
                )
        return {
            "replayed": True,
            "write_replayed": False,
            "run_id": data.get("run_id"),
            "task_id": data.get("task_id"),
            "final_state": data.get("state"),
            "status": data.get("status"),
            "event_count": len(events),
        }
