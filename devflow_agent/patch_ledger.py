"""Persistent, fail-closed M3 approval ledger and synthetic test runner.

The ledger is deliberately a control-plane primitive, not an Agent tool. It
stores exact patch bindings and one-time decisions in SQLite, while the runner
executes only a fixed ``pytest -q`` command in a copied synthetic workspace.
This is process containment for the demo workflow, not an OS sandbox claim.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .patches import (
    ApprovalDecision,
    PatchError,
    PatchProposal,
    PatchReceipt,
    apply_approved_patch,
)
from .repository import AuthorizedWorkspace


MAX_CAPTURE_CHARS = 4_000


class PatchLedgerError(PatchError):
    """A persistence or recovery error that must stop the workflow."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: str | None, limit: int = MAX_CAPTURE_CHARS) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


class PatchLedger:
    """SQLite-backed proposal, approval, receipt, and test-result ledger."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "patch ledger initialization failed"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _database(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    original_sha256 TEXT NOT NULL,
                    replacement_sha256 TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    unified_diff TEXT NOT NULL,
                    replacement_text TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
                    reviewer_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    proposal_id TEXT PRIMARY KEY REFERENCES proposals(proposal_id),
                    relative_path TEXT NOT NULL,
                    destination_root TEXT NOT NULL,
                    before_sha256 TEXT NOT NULL,
                    after_sha256 TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    write_count INTEGER NOT NULL,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_runs (
                    test_run_id TEXT PRIMARY KEY,
                    proposal_id TEXT REFERENCES proposals(proposal_id),
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    stdout_tail TEXT NOT NULL,
                    stderr_tail TEXT NOT NULL,
                    output_truncated INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    pid INTEGER,
                    cleanup_json TEXT NOT NULL,
                    error_type TEXT,
                    error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    proposal_id TEXT REFERENCES proposals(proposal_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _event(
        self,
        db: sqlite3.Connection,
        proposal_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO events(event_id, proposal_id, event_type, payload_json, created_at) VALUES(?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                proposal_id,
                event_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _utc(),
            ),
        )

    def record_proposal(self, proposal: PatchProposal) -> None:
        try:
            with self._database() as db:
                db.execute(
                    """
                    INSERT INTO proposals(
                        proposal_id, task_id, run_id, workspace_id, relative_path,
                        original_sha256, replacement_sha256, patch_sha256,
                        unified_diff, replacement_text, summary, status, consumed, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.task_id,
                        proposal.run_id,
                        proposal.workspace_id,
                        proposal.relative_path,
                        proposal.original_sha256,
                        proposal.replacement_sha256,
                        proposal.patch_sha256,
                        proposal.unified_diff,
                        proposal.replacement_text,
                        proposal.summary,
                        proposal.status,
                        int(proposal.consumed),
                        proposal.created_at,
                    ),
                )
                self._event(
                    db, proposal.proposal_id, "PROPOSAL_CREATED", proposal.to_dict()
                )
        except sqlite3.IntegrityError as exc:
            raise PatchLedgerError(
                "duplicate_proposal", "proposal already exists in the ledger"
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "proposal could not be persisted"
            ) from exc

    def load_proposal(self, proposal_id: str) -> PatchProposal:
        try:
            with self._database() as db:
                row = db.execute(
                    "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "proposal could not be loaded"
            ) from exc
        if row is None:
            raise PatchLedgerError(
                "proposal_not_found", "proposal is not present in the ledger"
            )
        return PatchProposal(
            proposal_id=row["proposal_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            relative_path=row["relative_path"],
            original_sha256=row["original_sha256"],
            replacement_sha256=row["replacement_sha256"],
            patch_sha256=row["patch_sha256"],
            unified_diff=row["unified_diff"],
            replacement_text=row["replacement_text"],
            summary=row["summary"],
            status=row["status"],
            consumed=bool(row["consumed"]),
            created_at=row["created_at"],
        )

    def _decide(
        self, proposal: PatchProposal, reviewer_id: str, decision: str
    ) -> ApprovalDecision:
        if not reviewer_id or not reviewer_id.strip():
            raise PatchLedgerError(
                "invalid_reviewer", "an independent reviewer identity is required"
            )
        try:
            with self._database() as db:
                row = db.execute(
                    "SELECT status, consumed, patch_sha256 FROM proposals WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()
                if row is None:
                    raise PatchLedgerError(
                        "proposal_not_found", "proposal is not present in the ledger"
                    )
                if row["status"] != "PENDING_APPROVAL" or row["consumed"]:
                    raise PatchLedgerError(
                        "invalid_proposal_state",
                        "only a pending proposal can be decided",
                    )
                if row["patch_sha256"] != proposal.patch_sha256:
                    raise PatchLedgerError(
                        "approval_binding_mismatch",
                        "proposal digest does not match the ledger",
                    )
                decided_at = _utc()
                db.execute(
                    "INSERT INTO decisions(proposal_id, reviewer_id, decision, patch_sha256, decided_at) VALUES(?,?,?,?,?)",
                    (
                        proposal.proposal_id,
                        reviewer_id.strip(),
                        decision,
                        proposal.patch_sha256,
                        decided_at,
                    ),
                )
                db.execute(
                    "UPDATE proposals SET status=? WHERE proposal_id=?",
                    (decision, proposal.proposal_id),
                )
                self._event(
                    db,
                    proposal.proposal_id,
                    "APPROVAL_DECISION",
                    {
                        "reviewer_id": reviewer_id.strip(),
                        "decision": decision,
                        "patch_sha256": proposal.patch_sha256,
                    },
                )
        except PatchLedgerError:
            raise
        except sqlite3.IntegrityError as exc:
            raise PatchLedgerError(
                "duplicate_decision", "a proposal can receive only one decision"
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "approval decision could not be persisted"
            ) from exc
        proposal.status = decision
        return ApprovalDecision(
            proposal.proposal_id,
            reviewer_id.strip(),
            decision,
            proposal.patch_sha256,
            decided_at,
        )

    def approve(self, proposal: PatchProposal, *, reviewer_id: str) -> ApprovalDecision:
        return self._decide(proposal, reviewer_id, "APPROVED")

    def reject(self, proposal: PatchProposal, *, reviewer_id: str) -> ApprovalDecision:
        return self._decide(proposal, reviewer_id, "REJECTED")

    def load_decision(self, proposal_id: str) -> ApprovalDecision:
        try:
            with self._database() as db:
                row = db.execute(
                    "SELECT * FROM decisions WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "approval decision could not be loaded"
            ) from exc
        if row is None:
            raise PatchLedgerError(
                "decision_not_found", "proposal has no persisted approval decision"
            )
        return ApprovalDecision(
            row["proposal_id"],
            row["reviewer_id"],
            row["decision"],
            row["patch_sha256"],
            row["decided_at"],
        )

    def apply_approved(
        self,
        proposal: PatchProposal,
        approval: ApprovalDecision,
        *,
        source_workspace: AuthorizedWorkspace,
        destination_root: str | os.PathLike[str],
    ) -> PatchReceipt:
        """Apply once, with an APPLYING checkpoint that prevents replay after crashes."""
        try:
            with self._database() as db:
                row = db.execute(
                    "SELECT status, consumed, patch_sha256 FROM proposals WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()
                if row is None:
                    raise PatchLedgerError(
                        "proposal_not_found", "proposal is not present in the ledger"
                    )
                if row["status"] != "APPROVED" or row["consumed"]:
                    raise PatchLedgerError(
                        "approval_required_or_consumed",
                        "a fresh approved proposal is required",
                    )
                if (
                    row["patch_sha256"] != approval.patch_sha256
                    or approval.decision != "APPROVED"
                ):
                    raise PatchLedgerError(
                        "approval_binding_mismatch",
                        "approval does not bind this exact proposal",
                    )
                db.execute(
                    "UPDATE proposals SET status='APPLYING' WHERE proposal_id=?",
                    (proposal.proposal_id,),
                )
                self._event(
                    db,
                    proposal.proposal_id,
                    "APPLYING",
                    {"destination_root": str(destination_root)},
                )
        except PatchLedgerError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "apply checkpoint could not be persisted"
            ) from exc

        try:
            receipt = apply_approved_patch(
                proposal,
                approval,
                source_workspace=source_workspace,
                destination_root=destination_root,
            )
        except Exception as exc:
            try:
                with self._database() as db:
                    db.execute(
                        "UPDATE proposals SET status='APPLY_FAILED', consumed=1 WHERE proposal_id=?",
                        (proposal.proposal_id,),
                    )
                    self._event(
                        db,
                        proposal.proposal_id,
                        "APPLY_FAILED",
                        {"error_type": type(exc).__name__, "message": str(exc)[:500]},
                    )
            except (OSError, sqlite3.Error) as persist_exc:
                raise PatchLedgerError(
                    "persistence_error",
                    "apply failed and failure state could not be persisted",
                ) from persist_exc
            if isinstance(exc, PatchError):
                raise
            raise PatchLedgerError(
                "apply_failed", "approved patch application failed"
            ) from exc

        try:
            with self._database() as db:
                db.execute(
                    "INSERT INTO receipts(proposal_id, relative_path, destination_root, before_sha256, after_sha256, patch_sha256, write_count, applied_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        receipt.proposal_id,
                        receipt.relative_path,
                        receipt.destination_root,
                        receipt.before_sha256,
                        receipt.after_sha256,
                        receipt.patch_sha256,
                        receipt.write_count,
                        receipt.applied_at,
                    ),
                )
                db.execute(
                    "UPDATE proposals SET status='APPLIED', consumed=1 WHERE proposal_id=?",
                    (proposal.proposal_id,),
                )
                self._event(db, proposal.proposal_id, "APPLIED", receipt.to_dict())
        except (OSError, sqlite3.Error) as exc:
            # The file may already have changed. Never replay it automatically.
            try:
                with self._database() as db:
                    db.execute(
                        "UPDATE proposals SET status='RESULT_UNKNOWN', consumed=1 WHERE proposal_id=?",
                        (proposal.proposal_id,),
                    )
                    self._event(
                        db,
                        proposal.proposal_id,
                        "RESULT_UNKNOWN",
                        {"error_type": type(exc).__name__},
                    )
            except (OSError, sqlite3.Error):
                pass
            raise PatchLedgerError(
                "result_unknown",
                "file application completed without a committed receipt",
            ) from exc
        proposal.status = "APPLIED"
        proposal.consumed = True
        return receipt

    def record_test_result(
        self, result: dict[str, Any], *, phase: str, proposal_id: str | None = None
    ) -> str:
        test_run_id = str(result.get("test_run_id") or uuid.uuid4())
        stdout_tail, stdout_truncated = _bounded(str(result.get("stdout_tail") or ""))
        stderr_tail, stderr_truncated = _bounded(str(result.get("stderr_tail") or ""))
        cleanup = (
            result.get("cleanup") if isinstance(result.get("cleanup"), dict) else {}
        )
        try:
            with self._database() as db:
                db.execute(
                    "INSERT INTO test_runs(test_run_id, proposal_id, phase, status, exit_code, stdout_tail, stderr_tail, output_truncated, started_at, ended_at, elapsed_seconds, pid, cleanup_json, error_type, error_message) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        test_run_id,
                        proposal_id,
                        phase,
                        str(result.get("status", "unknown")),
                        result.get("exit_code"),
                        stdout_tail,
                        stderr_tail,
                        int(stdout_truncated or stderr_truncated),
                        str(result.get("started_at", _utc())),
                        str(result.get("ended_at", _utc())),
                        float(result.get("elapsed_seconds", 0.0)),
                        result.get("pid"),
                        json.dumps(cleanup, ensure_ascii=False, sort_keys=True),
                        result.get("error_type"),
                        str(result.get("error_message", ""))[:500] or None,
                    ),
                )
                self._event(
                    db,
                    proposal_id,
                    "TEST_RESULT",
                    {
                        "test_run_id": test_run_id,
                        "phase": phase,
                        "status": result.get("status"),
                        "exit_code": result.get("exit_code"),
                    },
                )
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "test result could not be persisted"
            ) from exc
        return test_run_id

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        try:
            with self._database() as db:
                proposals = [
                    dict(row)
                    for row in db.execute(
                        "SELECT proposal_id, task_id, run_id, workspace_id, relative_path, original_sha256, replacement_sha256, patch_sha256, status, consumed, created_at FROM proposals ORDER BY created_at"
                    )
                ]
                decisions = [
                    dict(row)
                    for row in db.execute("SELECT * FROM decisions ORDER BY decided_at")
                ]
                receipts = [
                    dict(row)
                    for row in db.execute("SELECT * FROM receipts ORDER BY applied_at")
                ]
                test_runs = [
                    dict(row)
                    for row in db.execute(
                        "SELECT test_run_id, proposal_id, phase, status, exit_code, output_truncated, elapsed_seconds, pid, cleanup_json, error_type FROM test_runs ORDER BY started_at"
                    )
                ]
                events = [
                    dict(row)
                    for row in db.execute(
                        "SELECT event_id, proposal_id, event_type, created_at FROM events ORDER BY created_at"
                    )
                ]
        except (OSError, sqlite3.Error) as exc:
            raise PatchLedgerError(
                "persistence_error", "ledger snapshot could not be read"
            ) from exc
        return {
            "proposals": proposals,
            "decisions": decisions,
            "receipts": receipts,
            "test_runs": test_runs,
            "events": events,
        }


def _terminate_process_tree(process: subprocess.Popen[str]) -> dict[str, Any]:
    cleanup: dict[str, Any] = {
        "parent_pid": process.pid,
        "tree_termination_attempted": False,
        "tree_termination_exit_code": None,
        "remaining_parent": False,
    }
    if process.poll() is not None:
        return cleanup
    cleanup["tree_termination_attempted"] = True
    if os.name == "nt":
        try:
            ended = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            cleanup["tree_termination_exit_code"] = ended.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup["tree_termination_error"] = type(exc).__name__
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            cleanup["tree_termination_exit_code"] = 0
        except (OSError, ProcessLookupError) as exc:
            cleanup["tree_termination_error"] = type(exc).__name__
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        cleanup["remaining_parent"] = True
    return cleanup


def run_isolated_pytest(
    source_root: str | os.PathLike[str],
    *,
    destination_root: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Copy a synthetic repository and run only ``pytest -q`` with bounded capture."""
    source = Path(source_root).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise PatchLedgerError("invalid_source_root", "test source must be a directory")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise PatchLedgerError(
            "invalid_test_timeout", "test timeout must be between 0 and 300 seconds"
        )
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if destination_root is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="devflow-m3-isolated-")
        destination = Path(owned_temp.name) / "repo"
    else:
        destination = Path(destination_root).expanduser().resolve()
    if destination == source or destination.exists():
        if owned_temp is not None and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        raise PatchLedgerError(
            "invalid_isolation_root",
            "destination must be a distinct non-existing directory",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    command = [shutil.which("pytest") or sys.executable]
    if command[0] == sys.executable:
        command += ["-m", "pytest"]
    command += ["-q"]
    started = time.monotonic()
    started_at = _utc()
    process: subprocess.Popen[str] | None = None
    cleanup: dict[str, Any] = {
        "parent_pid": None,
        "tree_termination_attempted": False,
        "remaining_parent": False,
    }
    status = "unknown"
    exit_code: int | None = None
    stdout = stderr = ""
    error_type = error_message = None
    try:
        kwargs: dict[str, Any] = {
            "cwd": destination,
            "env": {**os.environ, "PYTHONPATH": str(destination)},
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        cleanup["parent_pid"] = process.pid
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code = process.returncode
            status = "passed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            cleanup = _terminate_process_tree(process)
            tail_out, _ = _bounded(
                stdout if isinstance(stdout, str) else stdout.decode(errors="replace")
            )
            tail_err, _ = _bounded(
                stderr if isinstance(stderr, str) else stderr.decode(errors="replace")
            )
            stdout, stderr = tail_out, tail_err
            status = "timeout"
            exit_code = None
        except Exception as exc:
            error_type, error_message = type(exc).__name__, str(exc)[:500]
            cleanup = _terminate_process_tree(process)
            status = "failed"
    except Exception as exc:
        error_type, error_message = type(exc).__name__, str(exc)[:500]
        status = "failed"
    finally:
        if process is not None and process.poll() is None:
            cleanup = _terminate_process_tree(process)
        if owned_temp is not None:
            owned_temp.cleanup()
    stdout_tail, stdout_truncated = _bounded(stdout)
    stderr_tail, stderr_truncated = _bounded(stderr)
    return {
        "test_run_id": str(uuid.uuid4()),
        "command": command,
        "destination_root": str(destination),
        "status": status,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "output_truncated": stdout_truncated or stderr_truncated,
        "started_at": started_at,
        "ended_at": _utc(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "pid": cleanup.get("parent_pid"),
        "cleanup": cleanup,
        "error_type": error_type,
        "error_message": error_message,
    }
