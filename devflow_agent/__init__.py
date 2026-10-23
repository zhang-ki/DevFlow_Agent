"""Read-only local repository analysis primitives."""

from .patch_ledger import PatchLedger, PatchLedgerError, run_isolated_pytest
from .patches import (
    ApprovalDecision,
    PatchError,
    PatchProposal,
    PatchReceipt,
    apply_approved_patch,
    approve_patch,
    create_patch_proposal,
    reject_patch,
)
from .repository import (
    AuthorizedWorkspace,
    FileRecord,
    ReadEvidence,
    RepoError,
    RepoErrorCode,
    RepositoryIndex,
    SearchHit,
    SymbolRecord,
    WorkspaceLimits,
    build_index,
)


__all__ = [
    "AuthorizedWorkspace",
    "FileRecord",
    "ReadEvidence",
    "RepoError",
    "RepoErrorCode",
    "RepositoryIndex",
    "SearchHit",
    "SymbolRecord",
    "WorkspaceLimits",
    "build_index",
    "ApprovalDecision",
    "PatchError",
    "PatchProposal",
    "PatchReceipt",
    "apply_approved_patch",
    "approve_patch",
    "create_patch_proposal",
    "reject_patch",
    "PatchLedger",
    "PatchLedgerError",
    "run_isolated_pytest",
]
