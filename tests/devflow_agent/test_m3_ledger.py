from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from shutil import copytree

from devflow_agent.patch_ledger import PatchLedger, PatchLedgerError, run_isolated_pytest
from devflow_agent.patches import create_patch_proposal
from devflow_agent.repository import AuthorizedWorkspace


class PersistentPatchLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="devflow-m3-ledger-")
        root = Path(self.temp.name)
        self.source = root / "source"
        (self.source / "src").mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "src" / "__init__.py").write_text("", encoding="utf-8")
        self.file = self.source / "src" / "totals.py"
        self.file.write_text("def total(values):\n    return 0\n", encoding="utf-8")
        (self.source / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
            encoding="utf-8",
        )
        self.workspace = AuthorizedWorkspace(self.source)
        self.original_sha = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.ledger_path = root / "ledger.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _proposal(self):
        return create_patch_proposal(
            self.workspace,
            "src/totals.py",
            "def total(values):\n    return sum(values)\n",
            expected_original_sha256=self.original_sha,
            task_id="task-persisted",
            run_id="run-persisted-1",
        )

    def test_decision_and_receipt_survive_reopen_and_apply_once(self) -> None:
        first = PatchLedger(self.ledger_path)
        proposal = self._proposal()
        first.record_proposal(proposal)
        reopened = PatchLedger(self.ledger_path)
        loaded = reopened.load_proposal(proposal.proposal_id)
        decision = reopened.approve(loaded, reviewer_id="reviewer-independent")
        reloaded = PatchLedger(self.ledger_path)
        loaded_again = reloaded.load_proposal(proposal.proposal_id)
        decision_again = reloaded.load_decision(proposal.proposal_id)
        isolated = Path(self.temp.name) / "isolated"
        copytree(self.source, isolated)
        receipt = reloaded.apply_approved(
            loaded_again,
            decision_again,
            source_workspace=self.workspace,
            destination_root=isolated,
        )
        self.assertEqual(decision.decision, "APPROVED")
        self.assertEqual(receipt.write_count, 1)
        self.assertEqual(hashlib.sha256(self.file.read_bytes()).hexdigest(), self.original_sha)
        self.assertEqual((isolated / "src" / "totals.py").read_text(encoding="utf-8").splitlines()[1], "    return sum(values)")
        with self.assertRaises(PatchLedgerError):
            reloaded.apply_approved(loaded_again, decision_again, source_workspace=self.workspace, destination_root=isolated)
        snapshot = reloaded.snapshot()
        self.assertEqual(snapshot["proposals"][0]["status"], "APPLIED")
        self.assertEqual(snapshot["proposals"][0]["consumed"], 1)
        self.assertEqual(len(snapshot["receipts"]), 1)
        self.assertTrue(any(event["event_type"] == "APPLIED" for event in snapshot["events"]))

    def test_persistence_failure_is_fail_closed(self) -> None:
        ledger = PatchLedger(self.ledger_path)
        proposal = self._proposal()
        original_connect = ledger._connect
        ledger._connect = lambda: (_ for _ in ()).throw(__import__("sqlite3").OperationalError("locked"))
        with self.assertRaises(PatchLedgerError) as raised:
            ledger.record_proposal(proposal)
        self.assertEqual(raised.exception.code, "persistence_error")
        ledger._connect = original_connect

    def test_test_results_are_bounded_and_persisted(self) -> None:
        ledger = PatchLedger(self.ledger_path)
        record_id = ledger.record_test_result(
            {"status": "failed", "exit_code": 1, "stdout_tail": "x" * 9000, "stderr_tail": "", "elapsed_seconds": 1.2},
            phase="baseline",
        )
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["test_runs"][0]["test_run_id"], record_id)
        self.assertEqual(snapshot["test_runs"][0]["status"], "failed")
        self.assertEqual(snapshot["test_runs"][0]["exit_code"], 1)
        self.assertEqual(snapshot["test_runs"][0]["output_truncated"], 1)


class IsolatedPytestRunnerTests(unittest.TestCase):
    def test_runner_records_pass_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory(prefix="devflow-m3-runner-") as temp:
            root = Path(temp) / "source"
            (root / "tests").mkdir(parents=True)
            (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
            passed = run_isolated_pytest(root, destination_root=Path(temp) / "pass-copy", timeout_seconds=30)
            self.assertEqual(passed["status"], "passed")
            self.assertEqual(passed["exit_code"], 0)
            slow = Path(temp) / "slow-source"
            (slow / "tests").mkdir(parents=True)
            (slow / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(2)\n", encoding="utf-8")
            timed_out = run_isolated_pytest(slow, destination_root=Path(temp) / "timeout-copy", timeout_seconds=0.2)
            self.assertEqual(timed_out["status"], "timeout")
            self.assertIsNone(timed_out["exit_code"])
            self.assertFalse(timed_out["cleanup"].get("remaining_parent", False))


if __name__ == "__main__":
    unittest.main()
