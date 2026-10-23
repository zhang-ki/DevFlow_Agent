# M3 bounded patch preview and approval

Date: 2026-10-23
Branch: `devflow/m0-openmanus`
HEAD: `3309bf4e416fb1c74b008f3e86494439a31bad53`

## Result

`PARTIAL` for the overall M3 roadmap, with the diff/approval control-flow slice verified in a synthetic workspace. No production write tool is registered and no real user repository was modified.

## Implemented control flow

- `devflow_agent/patches.py` creates a diff-only `PatchProposal` from the existing trusted read API.
- The proposal binds `task_id`, `run_id`, `workspace_id`, normalized relative path, original file SHA-256, replacement SHA-256, patch SHA-256 and the exact unified diff.
- `approve_patch` and `reject_patch` require an independent reviewer identity. Approval does not itself write anything.
- `apply_approved_patch` requires a fresh matching approval, rechecks the source and destination file digests, rejects path traversal and source-root targets, atomically writes only a distinct isolated copy, and consumes the proposal once.
- `devflow_agent/patch_ledger.py` adds a SQLite ledger for proposals, independent decisions, one-time receipts, state events, and bounded test results. It checkpoints `APPLYING` before the file operation; a failed or uncommitted completion is terminal and cannot be replayed automatically.
- `run_isolated_pytest` copies only a synthetic workspace, runs the fixed `pytest -q` command with a bounded timeout and bounded output capture, and records timeout/tree-termination evidence. This is a controlled process runner for the demo, not an OS sandbox claim.
- The module is not registered in `openmanus_tools.py`; the live Agent remains restricted to repository search and trusted reads.

## Evidence

`tests/devflow_agent/test_m3_patch_flow.py`: **5 passed, 0 failed, 0 skipped**, exit code `0`. Covered preview-without-write, rejection, approval and one-time isolated apply, stale source, path traversal and source-root rejection.

`tests/devflow_agent/test_m3_ledger.py`: **4 passed, 0 failed, 0 skipped**, exit code `0`. Covered approval/receipt recovery after reopening SQLite, duplicate-apply rejection, fail-closed persistence errors, bounded test-result capture, normal isolated pytest completion, and timeout cleanup.

The affected regression set (M1.5/M2 quality and diagnostics, read-only repository/Agent, OpenManus read-only ToolCollection, and M3 modules) ran **71 tests, 70 passed, 0 failed, 1 skipped**, exit code `0`. The single skip remains the host policy preventing temporary directory-junction creation; it is unrelated to the ledger change. Python compilation and `git diff --check` both exited `0`.

`scripts/demo_m3_repair.py --approve`: exit code `0`. In a temporary synthetic repository, baseline pytest returned exit code `1` (`assert 0 == 3`), the bounded read-only diagnostic returned `PASS`, a unified diff was shown, a separately named synthetic reviewer approved it, the proposal and decision were persisted, the diff was applied once to a temporary copy, and the isolated pytest returned exit code `0` (`1 passed`). The existing machine pytest runner was used (`<user-profile>\anaconda3\envs\mytorch\Scripts\pytest.EXE`; no dependency was installed). The source fixture SHA-256 before and after remained `09c7948b8ae6a627b9f670bb2d69bf1f71a69a0ff8f69f41bc83cc3277045388`. Raw output is preserved at `reports/m3_demo_runs/20261023T115131Z-m3-ledger-demo-final.log`; proposal id `3e8266b3-38e6-4cdc-a6aa-ee35497c48e7`, patch SHA-256 `43a301e784e124a60da901042b2e2b10b95a0ad99360e29803f34b0dd77b449d`.

The demo's candidate replacement is deterministic to prove the safety control plane. It is not model-generated evidence and is not an autonomous repair claim.

The updated end-to-end demo `scripts/demo_m3_repair.py --approve` completed at `reports/m3_demo_runs/20261023T115131Z-m3-ledger-demo-final.log` with exit code `0`. It recorded a real baseline pytest failure (`exit_code=1`), a persisted proposal and independent approval, an `APPLYING` → `APPLIED` event sequence, one receipt bound to the exact patch hash, and an isolated pytest pass (`exit_code=0`). The source workspace SHA-256 remained unchanged. The ledger was temporary synthetic data and was removed with the temporary run directory after the evidence log was written.

## Boundaries and next step

Persistent proposal, approval, receipt, event, and test-result records are now verified for the synthetic workflow. A real OS sandbox, model-generated patch selection, persistent cross-process recovery during a crash, failure-after-write replanning, and real user workspace writes remain `NOT_VERIFIED`. The ledger and runner remain outside the two-tool read-only Agent allowlist until a separate write-capability review.
