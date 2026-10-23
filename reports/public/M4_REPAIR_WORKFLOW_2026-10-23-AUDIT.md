# M4 证据驱动修复工作流独立复核

- 状态：**VERIFIED**（确定性的合成控制流范围）
- Run ID：`m4-20261023-audit-final2`
- Task ID：`task-m4-20261023-audit-final2`
- 状态序列：`CREATED -> BASELINE_TESTED -> DIAGNOSED -> PATCH_PROPOSED -> APPROVAL_PENDING -> APPROVED -> APPLIED -> VERIFYING -> COMPLETED`
- 诊断：`PASS`，可信证据条数：`2`
- 基线测试：`failed`，退出码 `1`，命令 `pytest -q`
- 修复后隔离测试：`passed`，退出码 `0`，命令 `pytest -q`
- 审批：`APPROVED`，Reviewer `synthetic-independent-reviewer`
- 补丁消费：`True`，写入次数由账本记录为一次
- 源工作区：运行前后聚合 SHA-256 一致，未修改；隔离副本清理：`True`
- 回放：只读核验状态、审批、哈希、测试收据、证据和清理证明；未执行写入或测试。
- 报告：[`run-report.json`](../m4_workflow_runs/m4-20261023-audit-final2/run-report.json)

本次没有启动模型、调用外部 API、执行 Task B 或写入真实用户仓库。结果验证的是确定性的合成诊断→Diff→独立审批→隔离应用→pytest 闭环。模型自主补丁、真实用户仓库写入、OS 级沙箱/Docker 隔离和失败后自主重规划仍为 **NOT_VERIFIED**。
