"""Bounded patch preview and approval primitives for synthetic workspaces.

This module deliberately does not register an OpenManus tool and never writes
the source workspace.  A proposal is created from a trusted current read,
approved by a separate caller, and can only be applied to a distinct isolated
workspace copy.  The checks are intentionally explicit so a future write
adapter cannot accidentally turn a preview into authorization.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repository import AuthorizedWorkspace, RepoError, RepoErrorCode


class PatchError(Exception):
    """Stable fail-closed error for patch preview, approval, or apply."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


@dataclass
class PatchProposal:
    proposal_id: str
    task_id: str
    run_id: str
    workspace_id: str
    relative_path: str
    original_sha256: str
    replacement_sha256: str
    patch_sha256: str
    unified_diff: str
    replacement_text: str = field(repr=False)
    summary: str = ""
    status: str = "PENDING_APPROVAL"
    consumed: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "relative_path": self.relative_path,
            "original_sha256": self.original_sha256,
            "replacement_sha256": self.replacement_sha256,
            "patch_sha256": self.patch_sha256,
            "unified_diff": self.unified_diff,
            "summary": self.summary,
            "status": self.status,
            "consumed": self.consumed,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ApprovalDecision:
    proposal_id: str
    reviewer_id: str
    decision: str
    patch_sha256: str
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "reviewer_id": self.reviewer_id,
            "decision": self.decision,
            "patch_sha256": self.patch_sha256,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class PatchReceipt:
    proposal_id: str
    relative_path: str
    destination_root: str
    before_sha256: str
    after_sha256: str
    patch_sha256: str
    write_count: int = 1
    applied_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "relative_path": self.relative_path,
            "destination_root": self.destination_root,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "patch_sha256": self.patch_sha256,
            "write_count": self.write_count,
            "applied_at": self.applied_at,
        }


def _full_text(workspace: AuthorizedWorkspace, relative_path: str) -> tuple[str, str, int]:
    """Read a complete stable text file through the existing trusted API."""
    try:
        preview = workspace.read_text(relative_path, start_line=1)
        if preview.line_count == 0:
            return "", preview.full_file_sha256, 0
        evidence = workspace.read_text(
            relative_path,
            start_line=1,
            end_line=preview.line_count,
            expected_sha256=preview.full_file_sha256,
        )
    except RepoError as exc:
        raise PatchError(exc.code.value if hasattr(exc.code, "value") else str(exc.code), exc.message, details=exc.details) from exc
    return evidence.content, evidence.full_file_sha256, evidence.line_count


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _patch_digest(unified_diff: str) -> str:
    return _digest(unified_diff.encode("utf-8"))


def create_patch_proposal(
    workspace: AuthorizedWorkspace,
    relative_path: str,
    replacement_text: str,
    *,
    expected_original_sha256: str,
    task_id: str,
    run_id: str,
    summary: str = "",
) -> PatchProposal:
    """Create a diff-only proposal after verifying the current file version."""
    if not task_id or not run_id:
        raise PatchError("invalid_identity", "task_id and run_id are required")
    if not isinstance(replacement_text, str) or "\x00" in replacement_text:
        raise PatchError("invalid_replacement", "replacement must be UTF-8 text without NUL bytes")
    if not isinstance(expected_original_sha256, str) or len(expected_original_sha256) != 64:
        raise PatchError("missing_original_digest", "a 64-character original SHA-256 is required")

    original_text, current_sha256, line_count = _full_text(workspace, relative_path)
    if current_sha256.casefold() != expected_original_sha256.casefold():
        raise PatchError(
            "stale_workspace_file",
            "the source file changed after its trusted read",
            details={"relative_path": relative_path, "expected_sha256": expected_original_sha256, "current_sha256": current_sha256},
        )
    parts = AuthorizedWorkspace.normalize_relative_path(relative_path)
    normalized = "/".join(parts)
    before = original_text.splitlines(keepends=True)
    after = replacement_text.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=normalized,
            tofile=normalized,
            lineterm="\n",
        )
    )
    return PatchProposal(
        proposal_id=str(uuid.uuid4()),
        task_id=task_id,
        run_id=run_id,
        workspace_id=workspace.workspace_id,
        relative_path=normalized,
        original_sha256=current_sha256,
        replacement_sha256=_digest(replacement_text.encode("utf-8")),
        patch_sha256=_patch_digest(diff),
        unified_diff=diff,
        replacement_text=replacement_text,
        summary=summary,
    )


def approve_patch(proposal: PatchProposal, *, reviewer_id: str) -> ApprovalDecision:
    if not reviewer_id or not reviewer_id.strip():
        raise PatchError("invalid_reviewer", "an independent reviewer identity is required")
    if proposal.status != "PENDING_APPROVAL" or proposal.consumed:
        raise PatchError("invalid_proposal_state", "only a pending proposal can be approved")
    proposal.status = "APPROVED"
    return ApprovalDecision(proposal.proposal_id, reviewer_id.strip(), "APPROVED", proposal.patch_sha256)


def reject_patch(proposal: PatchProposal, *, reviewer_id: str, reason: str = "") -> ApprovalDecision:
    if not reviewer_id or not reviewer_id.strip():
        raise PatchError("invalid_reviewer", "an independent reviewer identity is required")
    if proposal.status != "PENDING_APPROVAL" or proposal.consumed:
        raise PatchError("invalid_proposal_state", "only a pending proposal can be rejected")
    proposal.status = "REJECTED"
    return ApprovalDecision(proposal.proposal_id, reviewer_id.strip(), "REJECTED", proposal.patch_sha256)


def apply_approved_patch(
    proposal: PatchProposal,
    approval: ApprovalDecision,
    *,
    source_workspace: AuthorizedWorkspace,
    destination_root: str | os.PathLike[str],
) -> PatchReceipt:
    """Apply once to a distinct isolated copy, never to the source workspace."""
    if proposal.status != "APPROVED" or proposal.consumed:
        raise PatchError("approval_required_or_consumed", "a fresh approved proposal is required")
    if approval.proposal_id != proposal.proposal_id or approval.patch_sha256 != proposal.patch_sha256 or approval.decision != "APPROVED":
        raise PatchError("approval_binding_mismatch", "approval does not bind this exact proposal")
    if proposal.workspace_id != source_workspace.workspace_id:
        raise PatchError("workspace_binding_mismatch", "proposal belongs to a different workspace")
    source_text, source_sha256, _ = _full_text(source_workspace, proposal.relative_path)
    if source_sha256.casefold() != proposal.original_sha256.casefold():
        raise PatchError("stale_workspace_file", "source file changed after preview")
    try:
        destination = Path(destination_root).expanduser().resolve(strict=True)
        source_root = source_workspace.root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PatchError("invalid_isolation_root", "destination workspace could not be resolved") from exc
    if destination == source_root:
        raise PatchError("source_workspace_write_forbidden", "approved patches may only apply to a distinct isolated copy")
    try:
        destination_workspace = AuthorizedWorkspace(destination)
        destination_text, destination_sha256, _ = _full_text(destination_workspace, proposal.relative_path)
    except (RepoError, PatchError) as exc:
        if isinstance(exc, PatchError):
            raise
        raise PatchError("invalid_isolation_root", "destination workspace file could not be read") from exc
    if destination_sha256.casefold() != proposal.original_sha256.casefold():
        raise PatchError("destination_version_mismatch", "isolated copy is not the proposed source version")
    target = destination / Path(*AuthorizedWorkspace.normalize_relative_path(proposal.relative_path))
    if target.resolve(strict=False).parent != target.parent.resolve(strict=False):
        raise PatchError("path_outside_root", "destination path leaves the isolated workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=target.parent, delete=False) as stream:
            temp_name = stream.name
            stream.write(proposal.replacement_text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    after_sha256 = _digest(proposal.replacement_text.encode("utf-8"))
    proposal.status = "APPLIED"
    proposal.consumed = True
    return PatchReceipt(
        proposal_id=proposal.proposal_id,
        relative_path=proposal.relative_path,
        destination_root=str(destination),
        before_sha256=proposal.original_sha256,
        after_sha256=after_sha256,
        patch_sha256=proposal.patch_sha256,
    )
