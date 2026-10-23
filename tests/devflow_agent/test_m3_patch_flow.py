from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from shutil import copytree

from devflow_agent.patches import (
    PatchError,
    apply_approved_patch,
    approve_patch,
    create_patch_proposal,
    reject_patch,
)
from devflow_agent.repository import AuthorizedWorkspace


class PatchApprovalFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="devflow-m3-")
        self.root = Path(self.temp.name) / "source"
        (self.root / "src").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "src" / "totals.py").write_text(
            "def total(values):\n    return 0\n", encoding="utf-8"
        )
        (self.root / "tests" / "test_totals.py").write_text(
            "from src.totals import total\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
            encoding="utf-8",
        )
        self.workspace = AuthorizedWorkspace(self.root)
        self.path = "src/totals.py"
        self.original = (self.root / self.path).read_bytes()
        self.original_sha = hashlib.sha256(self.original).hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def proposal(self):
        return create_patch_proposal(
            self.workspace,
            self.path,
            "def total(values):\n    return sum(values)\n",
            expected_original_sha256=self.original_sha,
            task_id="task-m3",
            run_id="run-m3-1",
            summary="Use the input values instead of a constant.",
        )

    def test_preview_is_diff_only_and_does_not_write(self) -> None:
        proposal = self.proposal()
        self.assertEqual(proposal.status, "PENDING_APPROVAL")
        self.assertIn("-    return 0", proposal.unified_diff)
        self.assertIn("+    return sum(values)", proposal.unified_diff)
        self.assertEqual((self.root / self.path).read_bytes(), self.original)

    def test_rejection_is_fail_closed(self) -> None:
        proposal = self.proposal()
        decision = reject_patch(proposal, reviewer_id="independent-reviewer", reason="unsafe")
        self.assertEqual(decision.decision, "REJECTED")
        self.assertEqual(proposal.status, "REJECTED")
        self.assertEqual((self.root / self.path).read_bytes(), self.original)
        with self.assertRaisesRegex(PatchError, "pending proposal"):
            approve_patch(proposal, reviewer_id="reviewer")

    def test_approval_applies_once_to_isolated_copy(self) -> None:
        proposal = self.proposal()
        decision = approve_patch(proposal, reviewer_id="independent-reviewer")
        isolated = Path(self.temp.name) / "isolated"
        copytree(self.root, isolated)
        receipt = apply_approved_patch(
            proposal,
            decision,
            source_workspace=self.workspace,
            destination_root=isolated,
        )
        self.assertEqual(receipt.write_count, 1)
        self.assertEqual((self.root / self.path).read_bytes(), self.original)
        self.assertEqual(
            (isolated / self.path).read_text(encoding="utf-8"),
            "def total(values):\n    return sum(values)\n",
        )
        with self.assertRaisesRegex(PatchError, "approved proposal"):
            apply_approved_patch(proposal, decision, source_workspace=self.workspace, destination_root=isolated)

    def test_stale_source_and_destination_are_rejected(self) -> None:
        proposal = self.proposal()
        decision = approve_patch(proposal, reviewer_id="independent-reviewer")
        (self.root / self.path).write_text("def total(values):\n    return 1\n", encoding="utf-8")
        isolated = Path(self.temp.name) / "isolated"
        copytree(self.root, isolated)
        with self.assertRaisesRegex(PatchError, "source file changed"):
            apply_approved_patch(proposal, decision, source_workspace=self.workspace, destination_root=isolated)

    def test_path_traversal_and_source_destination_are_rejected(self) -> None:
        with self.assertRaises(PatchError):
            create_patch_proposal(
                self.workspace,
                "../outside.py",
                "x = 1\n",
                expected_original_sha256=self.original_sha,
                task_id="task",
                run_id="run",
            )
        proposal = self.proposal()
        decision = approve_patch(proposal, reviewer_id="independent-reviewer")
        with self.assertRaisesRegex(PatchError, "distinct isolated copy"):
            apply_approved_patch(proposal, decision, source_workspace=self.workspace, destination_root=self.root)


if __name__ == "__main__":
    unittest.main()
