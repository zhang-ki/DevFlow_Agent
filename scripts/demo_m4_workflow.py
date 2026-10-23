"""Run the deterministic M4 evidence-driven repair demonstration.

The demo creates a disposable synthetic repository, runs the real diagnostic,
creates a hash-bound proposal, and optionally applies it to an isolated copy
after an explicit synthetic reviewer decision.  It never touches a user
repository and never starts a model or registers a write-capable Agent tool.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# Make the checkout importable when invoked as ``python scripts/demo...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from devflow_agent.repair_workflow import RepairWorkflow


FAILURE_LOG = """FAILED tests/test_totals.py::test_total - assert 0 == 3
tests/test_totals.py:4: in test_total
src/totals.py:2: in total
"""


def _run_id() -> str:
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-m4-"
        + uuid.uuid4().hex[:8]
    )


def _build_synthetic_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "totals.py").write_text(
        "def total(values):\n    return 0\n", encoding="utf-8"
    )
    (root / "tests" / "test_totals.py").write_text(
        "from src.totals import total\n\n"
        "def test_total():\n    assert total([1, 2]) == 3\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the M4 synthetic repair workflow")
    parser.add_argument(
        "--approve", action="store_true", help="apply only to the isolated copy"
    )
    parser.add_argument("--reviewer-id", default="synthetic-independent-reviewer")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--report-root", default="reports/m4_workflow_runs")
    args = parser.parse_args()

    run_id = args.run_id or _run_id()
    task_id = args.task_id or f"task-{run_id}"
    run_dir = (Path(args.report_root) / run_id).resolve()
    if run_dir.exists():
        raise SystemExit(f"refusing to overwrite existing run directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    synthetic = run_dir / "synthetic_repo"
    _build_synthetic_repo(synthetic)
    workflow = RepairWorkflow(
        synthetic,
        ledger_path=run_dir / "patch-ledger.sqlite3",
        report_path=run_dir / "run-report.json",
        task_id=task_id,
        run_id=run_id,
        code_version="m4-demo",
    )
    replacement = "def total(values):\n    return sum(values)\n"
    report = workflow.execute(
        relative_path="src/totals.py",
        replacement_text=replacement,
        failure_log=FAILURE_LOG,
        summary="Use the input values instead of a constant.",
        decision="APPROVED" if args.approve else "NONE",
        reviewer_id=args.reviewer_id if args.approve else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\nEvidence report: {run_dir / 'run-report.json'}")
    print(
        "Source workspace is disposable synthetic data; no user repository was modified."
    )
    if args.approve:
        return 0 if report.get("status") == "VERIFIED" else 1
    return 0 if report.get("state") == "APPROVAL_PENDING" else 1


if __name__ == "__main__":
    sys.exit(main())
