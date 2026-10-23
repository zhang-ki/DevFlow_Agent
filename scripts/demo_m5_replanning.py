"""Run the deterministic M5 bounded-replanning demonstration.

The demo creates a synthetic repository under an explicit output directory.
It never writes to a user repository.  The first approved candidate is
intentionally insufficient; the second is bound to the resulting pytest
failure evidence and succeeds.  Approval is required on the command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# Make the checkout importable when invoked as ``python scripts/demo...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from devflow_agent.bounded_replanning import (
    ApprovalInstruction,
    BoundedReplanningWorkflow,
    RepairCandidate,
    ReplanLimits,
)


FAILURE_LOG = """FAILED tests/test_totals.py::test_total - assert 0 == 3
tests/test_totals.py:4: in test_total
src/totals.py:2: in total
"""


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _create_fixture(root: Path) -> Path:
    source = root / "synthetic-repository"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "__init__.py").write_text("", encoding="utf-8")
    (source / "src" / "totals.py").write_text(
        "def total(values):\n    return 0\n", encoding="utf-8"
    )
    (source / "tests" / "test_totals.py").write_text(
        "from src.totals import total\n\n"
        "def test_total():\n    assert total([1, 2]) == 3\n",
        encoding="utf-8",
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic M5 bounded-replanning demo."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--approve",
        action="store_true",
        help="explicitly approve each synthetic proposal",
    )
    parser.add_argument("--reviewer-id", default="synthetic-independent-reviewer")
    parser.add_argument("--run-id")
    parser.add_argument("--code-version", default="working-tree")
    args = parser.parse_args()

    output = args.output_dir.expanduser().resolve()
    if output.exists():
        parser.error("output directory already exists; choose a new evidence directory")
    output.mkdir(parents=True)
    source = _create_fixture(output)
    source_before = _tree_sha256(source)
    target = source / "src" / "totals.py"
    original_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-m5-"
        + uuid.uuid4().hex[:8]
    )
    workflow = BoundedReplanningWorkflow(
        source,
        report_path=output / "m5-run-report.json",
        initial_failure_log=FAILURE_LOG,
        allowed_paths=["src/totals.py"],
        task_id=f"task-{run_id}",
        run_id=run_id,
        limits=ReplanLimits(max_replans=1, test_timeout_seconds=30),
        code_version=args.code_version,
    )

    def candidate_provider(context):
        if context.attempt_number == 1:
            replacement = "def total(values):\n    return len(values)\n"
            reason = "The first bounded candidate uses the number of inputs."
        else:
            replacement = "def total(values):\n    return sum(values)\n"
            reason = "The new pytest evidence proves the values must be summed."
        return RepairCandidate(
            attempt_number=context.attempt_number,
            evidence_sha256=context.failure_evidence.fingerprint,
            relative_path="src/totals.py",
            expected_original_sha256=original_sha,
            replacement_text=replacement,
            reason=reason,
            summary=reason,
        )

    def approval_provider(_context, _preview):
        if not args.approve:
            return None
        return ApprovalInstruction("APPROVED", args.reviewer_id)

    report = workflow.execute(
        candidate_provider=candidate_provider,
        approval_provider=approval_provider,
    )
    source_after = _tree_sha256(source)
    replay = BoundedReplanningWorkflow.replay_report(output / "m5-run-report.json")
    summary = {
        "run_id": run_id,
        "state": report["state"],
        "status": report["status"],
        "stop_reason": report["stop_reason"],
        "attempt_count": len(report["attempts"]),
        "source_sha256_before": source_before,
        "source_sha256_after": source_after,
        "source_unchanged": source_before == source_after,
        "report": "m5-run-report.json",
        "replay": replay,
    }
    (output / "demo-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "VERIFIED" and source_before == source_after else 3


if __name__ == "__main__":
    sys.exit(main())
