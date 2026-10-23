"""Run the bounded M3 diagnosis -> diff -> approval -> isolated test demo.

The candidate replacement is deterministic so this demonstration exercises the
approval and evidence contracts without pretending to be a model-generated
patch.  It never writes the fixture source; ``--approve`` applies only to a
temporary isolated copy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path


# Make the checkout importable when invoked as ``python scripts/demo...``.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from devflow_agent.diagnostics import diagnose_failures
from devflow_agent.patch_ledger import PatchLedger, run_isolated_pytest
from devflow_agent.patches import create_patch_proposal
from devflow_agent.repository import AuthorizedWorkspace


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 synthetic patch approval demo")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="approve and apply only to a temporary isolated copy",
    )
    parser.add_argument(
        "--task-id",
        default="demo-m3-task",
        help="task identifier recorded in the synthetic ledger",
    )
    parser.add_argument(
        "--run-id",
        default="demo-m3-run",
        help="run identifier recorded in the synthetic ledger",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="devflow-m3-source-") as temp:
        fixture = Path(temp) / "repo"
        (fixture / "src").mkdir(parents=True)
        (fixture / "tests").mkdir()
        (fixture / "src" / "__init__.py").write_text("", encoding="utf-8")
        source = fixture / "src" / "totals.py"
        source.write_text("def total(values):\n    return 0\n", encoding="utf-8")
        (fixture / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
            encoding="utf-8",
        )
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        workspace = AuthorizedWorkspace(fixture)
        ledger = PatchLedger(Path(temp) / "execution-ledger.sqlite3")
        baseline = run_isolated_pytest(
            fixture, destination_root=Path(temp) / "baseline-copy", timeout_seconds=60
        )
        ledger.record_test_result(baseline, phase="baseline")
        # The fixture adds the implementation frame that a captured traceback
        # would contain in a real CI report; it remains synthetic and bounded.
        log = (
            str(baseline["stdout_tail"])
            + "\n"
            + str(baseline["stderr_tail"])
            + "\nsrc/totals.py:2: in total\n"
        )
        report = asyncio.run(
            asyncio.to_thread(
                diagnose_failures, workspace, log, exit_code=int(baseline["exit_code"])
            )
        )
        proposal = create_patch_proposal(
            workspace,
            "src/totals.py",
            "def total(values):\n    return sum(values)\n",
            expected_original_sha256=before,
            task_id=args.task_id,
            run_id=args.run_id,
            summary="Replace the constant with the tested sum operation.",
        )
        ledger.record_proposal(proposal)
        result: dict[str, object] = {
            "baseline_test": baseline,
            "diagnostic_status": report.status,
            "proposal": proposal.to_dict(),
            "approval": None,
            "isolated_test": None,
            "ledger_path": str(Path(temp) / "execution-ledger.sqlite3"),
            "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest()
            == before,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.approve:
            return 0
        decision = ledger.approve(
            proposal, reviewer_id="independent-synthetic-reviewer"
        )
        with tempfile.TemporaryDirectory(
            prefix="devflow-m3-isolated-"
        ) as isolated_temp:
            isolated = Path(isolated_temp) / "repo"
            shutil.copytree(fixture, isolated)
            receipt = ledger.apply_approved(
                proposal,
                decision,
                source_workspace=workspace,
                destination_root=isolated,
            )
            result["approval"] = decision.to_dict()
            result["proposal"] = proposal.to_dict()
            result["receipt"] = receipt.to_dict()
            result["isolated_test"] = run_isolated_pytest(isolated, timeout_seconds=60)
            ledger.record_test_result(
                result["isolated_test"],
                phase="post_apply",
                proposal_id=proposal.proposal_id,
            )
            result["ledger_snapshot"] = ledger.snapshot()
            result["source_unchanged"] = (
                hashlib.sha256(source.read_bytes()).hexdigest() == before
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            0
            if result["isolated_test"]["status"] == "passed"
            and result["source_unchanged"]
            else 3
        )


if __name__ == "__main__":
    raise SystemExit(main())
