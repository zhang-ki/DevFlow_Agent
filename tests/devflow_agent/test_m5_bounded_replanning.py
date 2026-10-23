from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_agent.bounded_replanning import (
    ApprovalInstruction,
    BoundedReplanningWorkflow,
    RepairCandidate,
    ReplanError,
    ReplanLimits,
)
from devflow_agent.patch_ledger import PatchLedgerError


FAILURE_LOG = """FAILED tests/test_totals.py::test_total - assert 0 == 3
tests/test_totals.py:4: in test_total
src/totals.py:2: in total
"""


class BoundedReplanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="devflow-m5-")
        root = Path(self.temp.name)
        self.source = root / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "src" / "__init__.py").write_text("", encoding="utf-8")
        self.target = self.source / "src" / "totals.py"
        self.target.write_text("def total(values):\n    return 0\n", encoding="utf-8")
        (self.source / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\n"
            "def test_total():\n    assert total([1, 2]) == 3\n",
            encoding="utf-8",
        )
        self.source_hash = self._tree_hash(self.source)
        self.original_sha = hashlib.sha256(self.target.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _tree_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def workflow(self, name: str, *, max_replans: int = 1) -> BoundedReplanningWorkflow:
        output = Path(self.temp.name) / name
        return BoundedReplanningWorkflow(
            self.source,
            report_path=output / "m5-report.json",
            initial_failure_log=FAILURE_LOG,
            allowed_paths=["src/totals.py"],
            task_id=f"task-{name}",
            run_id=f"run-{name}",
            limits=ReplanLimits(
                max_replans=max_replans,
                max_events=32,
                test_timeout_seconds=30,
            ),
            code_version="test-version",
        )

    def candidate(self, context, replacement: str, reason: str) -> RepairCandidate:
        return RepairCandidate(
            attempt_number=context.attempt_number,
            evidence_sha256=context.failure_evidence.fingerprint,
            relative_path="src/totals.py",
            expected_original_sha256=self.original_sha,
            replacement_text=replacement,
            reason=reason,
            summary=reason,
        )

    @staticmethod
    def approve(_context, _preview) -> ApprovalInstruction:
        return ApprovalInstruction("APPROVED", "independent-test-reviewer")

    def test_first_failure_replans_once_then_succeeds(self) -> None:
        workflow = self.workflow("success")

        def planner(context):
            replacement = (
                "def total(values):\n    return len(values)\n"
                if context.attempt_number == 1
                else "def total(values):\n    return sum(values)\n"
            )
            return self.candidate(
                context, replacement, f"attempt {context.attempt_number}"
            )

        report = workflow.execute(
            candidate_provider=planner, approval_provider=self.approve
        )
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["state"], "COMPLETED")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(report["attempts"][0]["m4_state"], "TEST_FAILED")
        self.assertEqual(report["attempts"][1]["m4_state"], "COMPLETED")
        self.assertEqual(report["attempts"][0]["receipt_count"], 1)
        self.assertEqual(report["attempts"][1]["receipt_count"], 1)
        self.assertEqual(
            report["attempts"][1]["input_evidence"]["fingerprint"],
            report["attempts"][0]["failure_evidence"]["fingerprint"],
        )
        self.assertEqual(self._tree_hash(self.source), self.source_hash)
        replay = BoundedReplanningWorkflow.replay_report(
            Path(self.temp.name) / "success" / "m5-report.json"
        )
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["write_replayed"])
        self.assertFalse(replay["tests_replayed"])
        self.assertEqual(replay["attempt_count"], 2)

    def test_distinct_failures_stop_at_replan_limit(self) -> None:
        workflow = self.workflow("exhausted")

        def planner(context):
            value = context.attempt_number
            return self.candidate(
                context,
                f"def total(values):\n    return {value}\n",
                f"bounded unsuccessful attempt {value}",
            )

        report = workflow.execute(
            candidate_provider=planner, approval_provider=self.approve
        )
        self.assertEqual(report["state"], "RETRY_EXHAUSTED")
        self.assertEqual(report["stop_reason"], "replan_limit_reached")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_repeated_failure_stops_without_using_remaining_budget(self) -> None:
        workflow = self.workflow("repeated", max_replans=2)
        replacements = {
            1: "def total(values):\n    return 0 if values else 0\n",
            2: "def total(values):\n    return int(False)\n",
            3: "def total(values):\n    return sum(values)\n",
        }

        def planner(context):
            return self.candidate(
                context,
                replacements[context.attempt_number],
                f"candidate {context.attempt_number}",
            )

        report = workflow.execute(
            candidate_provider=planner, approval_provider=self.approve
        )
        self.assertEqual(report["state"], "REPEATED_FAILURE")
        self.assertEqual(report["stop_reason"], "repeated_failure_without_new_evidence")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_missing_post_test_evidence_fails_closed(self) -> None:
        workflow = self.workflow("missing-evidence")
        baseline = {
            "test_run_id": "baseline",
            "status": "failed",
            "exit_code": 1,
            "stdout_tail": FAILURE_LOG,
            "stderr_tail": "",
            "output_truncated": False,
            "elapsed_seconds": 0.1,
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "pid": None,
            "cleanup": {"remaining_parent": False},
            "error_type": None,
            "error_message": None,
        }
        post = {
            **baseline,
            "test_run_id": "post",
            "stdout_tail": "",
            "stderr_tail": "",
        }

        def planner(context):
            return self.candidate(
                context,
                "def total(values):\n    return 99\n",
                "candidate with missing test diagnostics",
            )

        with patch(
            "devflow_agent.repair_workflow.run_isolated_pytest",
            side_effect=[baseline, post],
        ):
            report = workflow.execute(
                candidate_provider=planner, approval_provider=self.approve
            )
        self.assertEqual(report["state"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(report["stop_reason"], "missing_verification_failure_evidence")
        self.assertEqual(len(report["attempts"]), 1)

    def test_out_of_scope_candidate_is_rejected_before_proposal(self) -> None:
        workflow = self.workflow("scope")

        def planner(context):
            return RepairCandidate(
                attempt_number=context.attempt_number,
                evidence_sha256=context.failure_evidence.fingerprint,
                relative_path="tests/test_totals.py",
                expected_original_sha256=self.original_sha,
                replacement_text="assert True\n",
                reason="attempt to exceed allowed scope",
            )

        report = workflow.execute(
            candidate_provider=planner, approval_provider=self.approve
        )
        self.assertEqual(report["state"], "SAFETY_REJECTED")
        self.assertEqual(report["stop_reason"], "candidate_out_of_scope")
        self.assertEqual(report["attempts"], [])
        self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_replan_candidate_cannot_bind_stale_failure_evidence(self) -> None:
        workflow = self.workflow("stale-evidence")
        first_evidence: list[str] = []

        def planner(context):
            if context.attempt_number == 1:
                first_evidence.append(context.failure_evidence.fingerprint)
                return self.candidate(
                    context,
                    "def total(values):\n    return 1\n",
                    "first bounded candidate",
                )
            return RepairCandidate(
                attempt_number=context.attempt_number,
                evidence_sha256=first_evidence[0],
                relative_path="src/totals.py",
                expected_original_sha256=self.original_sha,
                replacement_text="def total(values):\n    return sum(values)\n",
                reason="candidate incorrectly bound to old evidence",
            )

        report = workflow.execute(
            candidate_provider=planner, approval_provider=self.approve
        )
        self.assertEqual(report["state"], "SAFETY_REJECTED")
        self.assertEqual(report["stop_reason"], "candidate_evidence_mismatch")
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_verification_timeout_is_terminal_and_not_replanned(self) -> None:
        workflow = self.workflow("timeout", max_replans=2)
        baseline = {
            "test_run_id": "baseline",
            "status": "failed",
            "exit_code": 1,
            "stdout_tail": FAILURE_LOG,
            "stderr_tail": "",
            "output_truncated": False,
            "elapsed_seconds": 0.1,
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
            "pid": None,
            "cleanup": {"remaining_parent": False},
            "error_type": None,
            "error_message": None,
        }
        post = {
            **baseline,
            "test_run_id": "post",
            "status": "timeout",
            "exit_code": None,
            "stdout_tail": "test execution exceeded its deadline",
        }

        def planner(context):
            return self.candidate(
                context,
                "def total(values):\n    return 99\n",
                "candidate whose verification times out",
            )

        with patch(
            "devflow_agent.repair_workflow.run_isolated_pytest",
            side_effect=[baseline, post],
        ):
            report = workflow.execute(
                candidate_provider=planner, approval_provider=self.approve
            )
        self.assertEqual(report["state"], "TEST_TIMEOUT")
        self.assertEqual(report["stop_reason"], "verification_timeout")
        self.assertEqual(len(report["attempts"]), 1)

    def test_missing_or_rejected_approval_never_applies_patch(self) -> None:
        for suffix, approval, expected_state in (
            ("missing", lambda _context, _preview: None, "APPROVAL_REQUIRED"),
            (
                "rejected",
                lambda _context, _preview: ApprovalInstruction(
                    "REJECTED", "independent-test-reviewer"
                ),
                "APPROVAL_REJECTED",
            ),
        ):
            with self.subTest(suffix=suffix):
                workflow = self.workflow(f"approval-{suffix}")

                def planner(context):
                    return self.candidate(
                        context,
                        "def total(values):\n    return sum(values)\n",
                        "valid patch still requires approval",
                    )

                report = workflow.execute(
                    candidate_provider=planner, approval_provider=approval
                )
                self.assertEqual(report["state"], expected_state)
                self.assertEqual(report["attempts"][0]["receipt_count"], 0)
                self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_ledger_failure_stops_before_approval(self) -> None:
        workflow = self.workflow("ledger")

        def planner(context):
            return self.candidate(
                context,
                "def total(values):\n    return sum(values)\n",
                "proposal persistence must succeed",
            )

        with patch(
            "devflow_agent.patch_ledger.PatchLedger.record_proposal",
            side_effect=PatchLedgerError("persistence_error", "simulated failure"),
        ):
            report = workflow.execute(
                candidate_provider=planner, approval_provider=self.approve
            )
        self.assertEqual(report["state"], "PERSISTENCE_ERROR")
        self.assertEqual(report["status"], "NOT_VERIFIED")
        self.assertEqual(report["attempts"][0]["receipt_count"], 0)
        self.assertEqual(self._tree_hash(self.source), self.source_hash)

    def test_replay_rejects_tampered_evidence_and_attempt_report(self) -> None:
        workflow = self.workflow("tamper")

        def planner(context):
            return self.candidate(
                context,
                "def total(values):\n    return sum(values)\n",
                "passing candidate",
            )

        workflow.execute(candidate_provider=planner, approval_provider=self.approve)
        path = Path(self.temp.name) / "tamper" / "m5-report.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        modified = json.loads(json.dumps(original))
        modified["attempts"][0]["candidate"]["evidence_sha256"] = "0" * 64
        changed = path.with_name("tampered-evidence.json")
        changed.write_text(json.dumps(modified), encoding="utf-8")
        with self.assertRaises(ReplanError):
            BoundedReplanningWorkflow.replay_report(changed)

        modified = json.loads(json.dumps(original))
        modified["attempts"][0]["attempt_report_sha256"] = "0" * 64
        changed = path.with_name("tampered-attempt.json")
        changed.write_text(json.dumps(modified), encoding="utf-8")
        with self.assertRaises(ReplanError):
            BoundedReplanningWorkflow.replay_report(changed)

        modified = json.loads(json.dumps(original))
        modified["attempts"][0]["receipt_count"] = 0
        changed = path.with_name("tampered-receipt.json")
        changed.write_text(json.dumps(modified), encoding="utf-8")
        with self.assertRaises(ReplanError):
            BoundedReplanningWorkflow.replay_report(changed)


if __name__ == "__main__":
    unittest.main()
