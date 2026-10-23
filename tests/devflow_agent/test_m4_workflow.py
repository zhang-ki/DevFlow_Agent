from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devflow_agent.patch_ledger import PatchLedgerError
from devflow_agent.repair_workflow import RepairWorkflow, WorkflowError, WorkflowLimits


FAILURE_LOG = """FAILED tests/test_totals.py::test_total - assert 0 == 3
tests/test_totals.py:4: in test_total
src/totals.py:2: in total
"""


class EvidenceDrivenWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="devflow-m4-")
        root = Path(self.temp.name)
        self.source = root / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "src" / "__init__.py").write_text("", encoding="utf-8")
        self.file = self.source / "src" / "totals.py"
        self.file.write_text("def total(values):\n    return 0\n", encoding="utf-8")
        (self.source / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\n"
            "def test_total():\n    assert total([1, 2]) == 3\n",
            encoding="utf-8",
        )
        self.original = hashlib.sha256(self.file.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def workflow(self, name: str) -> RepairWorkflow:
        root = Path(self.temp.name) / name
        root.mkdir()
        return RepairWorkflow(
            self.source,
            ledger_path=root / "ledger.sqlite3",
            report_path=root / "run-report.json",
            task_id="task-m4",
            run_id=f"run-{name}",
            limits=WorkflowLimits(test_timeout_seconds=30),
            code_version="test-version",
        )

    def kwargs(self) -> dict[str, str]:
        return {
            "relative_path": "src/totals.py",
            "replacement_text": "def total(values):\n    return sum(values)\n",
            "failure_log": FAILURE_LOG,
            "summary": "Use the values supplied by the caller.",
        }

    def test_preview_waits_for_explicit_approval_and_replay_is_read_only(self) -> None:
        workflow = self.workflow("preview")
        report = workflow.execute(**self.kwargs())
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["state"], "APPROVAL_PENDING")
        self.assertEqual(report["ledger"]["receipts"], [])
        self.assertEqual(
            hashlib.sha256(self.file.read_bytes()).hexdigest(), self.original
        )
        replay = RepairWorkflow.replay_report(
            Path(self.temp.name) / "preview" / "run-report.json"
        )
        self.assertFalse(replay["write_replayed"])
        self.assertEqual(replay["final_state"], "APPROVAL_PENDING")

    def test_approved_patch_isolated_and_verified_once(self) -> None:
        workflow = self.workflow("approved")
        report = workflow.execute(
            **self.kwargs(), decision="APPROVED", reviewer_id="independent-reviewer"
        )
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["state"], "COMPLETED")
        self.assertEqual(report["tests"]["baseline"]["exit_code"], 1)
        self.assertEqual(report["tests"]["post_apply"]["exit_code"], 0)
        self.assertEqual(len(report["ledger"]["receipts"]), 1)
        self.assertTrue(report["source_workspace"]["unchanged"])
        replay = RepairWorkflow.replay_report(
            Path(self.temp.name) / "approved" / "run-report.json"
        )
        self.assertFalse(replay["write_replayed"])

    def test_rejected_patch_does_not_write(self) -> None:
        workflow = self.workflow("rejected")
        report = workflow.execute(
            **self.kwargs(), decision="REJECTED", reviewer_id="independent-reviewer"
        )
        self.assertEqual(report["state"], "REJECTED")
        self.assertEqual(report["ledger"]["receipts"], [])
        self.assertEqual(
            self.file.read_text(encoding="utf-8"),
            "def total(values):\n    return 0\n",
        )

    def test_stale_source_after_preview_is_fail_closed(self) -> None:
        workflow = self.workflow("stale")
        preview = workflow.prepare(**self.kwargs())
        self.assertEqual(preview["state"], "APPROVAL_PENDING")
        self.file.write_text("def total(values):\n    return 1\n", encoding="utf-8")
        report = workflow.finalize(
            decision="APPROVED", reviewer_id="independent-reviewer"
        )
        self.assertEqual(report["state"], "STALE_WORKSPACE_FILE")
        self.assertEqual(report["ledger"]["receipts"], [])

    def test_path_traversal_is_rejected(self) -> None:
        workflow = self.workflow("path")
        report = workflow.prepare(
            relative_path="../outside.py",
            replacement_text="x = 1\n",
            failure_log=FAILURE_LOG,
        )
        self.assertNotEqual(report["status"], "VERIFIED")
        self.assertIn(report["state"], {"FAILED", "STALE_WORKSPACE_FILE"})

    def test_workspace_links_are_rejected_before_isolated_execution(self) -> None:
        outside = Path(self.temp.name) / "outside.py"
        outside.write_text("value = 99\n", encoding="utf-8")
        link = self.source / "src" / "external.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit creating a workspace symlink")
        workflow = self.workflow("link")
        report = workflow.prepare(**self.kwargs())
        self.assertEqual(report["state"], "FAILED")
        self.assertEqual(report["stop_reason"], "unsafe_workspace_link")
        self.assertEqual(report["ledger"]["proposals"], [])
        self.assertEqual(report["ledger"]["receipts"], [])
        self.assertEqual(
            hashlib.sha256(self.file.read_bytes()).hexdigest(), self.original
        )

    def test_report_is_json_and_binds_patch_hash_to_ledger(self) -> None:
        workflow = self.workflow("report")
        report = workflow.execute(**self.kwargs())
        path = Path(self.temp.name) / "report" / "run-report.json"
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["run_id"], "run-report")
        self.assertEqual(
            loaded["proposal"]["patch_sha256"],
            loaded["ledger"]["proposals"][0]["patch_sha256"],
        )
        self.assertEqual(
            report["source_workspace"]["before"], report["source_workspace"]["after"]
        )

    def test_ledger_persistence_failure_stops_before_approval(self) -> None:
        workflow = self.workflow("persistence")

        def fail_record(_proposal: object) -> None:
            raise PatchLedgerError("persistence_error", "simulated ledger failure")

        workflow.ledger.record_proposal = fail_record  # type: ignore[method-assign]
        report = workflow.prepare(**self.kwargs())
        self.assertEqual(report["state"], "PERSISTENCE_ERROR")
        self.assertEqual(report["status"], "NOT_VERIFIED")
        self.assertEqual(report["ledger"]["proposals"], [])
        self.assertEqual(
            hashlib.sha256(self.file.read_bytes()).hexdigest(), self.original
        )

    def test_post_apply_failure_is_recorded_without_source_write(self) -> None:
        workflow = self.workflow("post-fail")
        report = workflow.execute(
            **{
                **self.kwargs(),
                "replacement_text": "def total(values):\n    return 99\n",
            },
            decision="APPROVED",
            reviewer_id="independent-reviewer",
        )
        self.assertEqual(report["state"], "TEST_FAILED")
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["tests"]["post_apply"]["exit_code"], 1)
        self.assertTrue(report["source_workspace"]["unchanged"])

    def test_second_finalize_cannot_replay_consumed_patch(self) -> None:
        workflow = self.workflow("replay")
        first = workflow.execute(
            **self.kwargs(), decision="APPROVED", reviewer_id="independent-reviewer"
        )
        second = workflow.finalize(
            decision="APPROVED", reviewer_id="independent-reviewer"
        )
        self.assertEqual(first["status"], "VERIFIED")
        self.assertEqual(second["state"], "COMPLETED")
        self.assertEqual(len(second["ledger"]["receipts"]), 1)

    def test_replay_rejects_tampered_state_hash_or_cleanup_evidence(self) -> None:
        workflow = self.workflow("tamper")
        workflow.execute(
            **self.kwargs(), decision="APPROVED", reviewer_id="independent-reviewer"
        )
        path = Path(self.temp.name) / "tamper" / "run-report.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            {"status": "PARTIAL"},
            {"proposal": {**original["proposal"], "patch_sha256": "0" * 64}},
            {"isolated_copy_cleanup": False},
        )
        for index, mutation in enumerate(mutations, 1):
            with self.subTest(mutation=mutation):
                candidate = {**original, **mutation}
                candidate_path = path.with_name(f"run-report-tampered-{index}.json")
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(WorkflowError):
                    RepairWorkflow.replay_report(candidate_path)

    def test_isolated_cleanup_failure_cannot_be_verified(self) -> None:
        workflow = self.workflow("cleanup-failure")
        leaked: list[Path] = []

        def refuse_cleanup(path: Path) -> None:
            leaked.append(path)

        with patch.object(
            RepairWorkflow, "_cleanup_isolated", side_effect=refuse_cleanup
        ):
            report = workflow.execute(
                **self.kwargs(),
                decision="APPROVED",
                reviewer_id="independent-reviewer",
            )
        self.assertEqual(report["state"], "TEST_FAILED")
        self.assertEqual(report["stop_reason"], "isolated_copy_cleanup_failed")
        self.assertFalse(report["isolated_copy_cleanup"])
        self.assertEqual(report["status"], "PARTIAL")
        self.assertTrue(report["source_workspace"]["unchanged"])
        for path in leaked:
            shutil.rmtree(path, ignore_errors=True)

    def test_baseline_timeout_stops_before_diagnosis_or_approval(self) -> None:
        slow = self.source / "tests" / "test_slow.py"
        slow.write_text(
            "import time\n\ndef test_slow():\n    time.sleep(2)\n",
            encoding="utf-8",
        )
        workflow = RepairWorkflow(
            self.source,
            ledger_path=Path(self.temp.name) / "timeout" / "ledger.sqlite3",
            report_path=Path(self.temp.name) / "timeout" / "run-report.json",
            task_id="task-timeout",
            run_id="run-timeout",
            limits=WorkflowLimits(test_timeout_seconds=0.2),
        )
        report = workflow.prepare(**self.kwargs())
        self.assertEqual(report["state"], "FAILED")
        self.assertEqual(report["tests"]["baseline"]["status"], "timeout")
        self.assertIsNone(report["tests"]["baseline"]["exit_code"])


if __name__ == "__main__":
    unittest.main()
