# M1–M3 面试演示交付记录（2026-10-23）

## 范围

本轮冻结现有 M1–M3 能力，只增加一个有界演示入口和面试说明。没有启动模型、调用外部 API、运行 Task B、修改真实工作区或覆盖历史运行证据。

入口：`scripts/run_interview_demo.ps1`

- `-Demo check`：检查项目虚拟环境、只读模块、配置、历史证据和 11434 监听状态；不发请求。
- `-Demo m2`：创建新的摘要目录，读取保存的真实 Qwen3.5-4B M2 运行和离线重评；不重新启动模型。由于历史数值质量为 `UNVERIFIABLE`，该入口按预期返回非零状态并保留 `PARTIAL`。
- `-Demo m3`：运行一次确定性的合成仓库诊断→Diff→独立 Reviewer→SQLite→隔离副本测试流程。

## 实际运行

### M2 保存证据展示

- 运行 ID：`interview-m2-20261023T122226Z`（另一次同样只读的摘要为 `interview-m2-20261023T122235Z`）。
- 启动器退出码：3（历史质量未通过，不是脚本崩溃）。
- 模型重启：否；当前监听：否。
- 来源运行：`20261023T-m2-qwen35-4b-r6-ctx4096`。
- 工具轨迹：搜索 1 次、可信读取 2 次，调用 ID 和结果均来自保存记录。
- 引用：`PASS`；数值：`UNVERIFIABLE`；整体：`PARTIAL`。
- 证据：`reports/interview_demo_runs/interview-m2-20261023T122226Z/m2-demo-summary.json`。

这条路径展示真实模型曾完成的过程和真实限制，不能描述为新的模型运行或 M2 完整通过。

### M3 合成修复闭环

- 运行 ID：`interview-m3-20261023T122410Z`。
- 启动器退出码：0。
- 基线测试：`failed`，退出码 1。
- 诊断：`PASS`。
- 审批：1 次，Reviewer `independent-synthetic-reviewer`。
- 应用：1 次，SQLite 一次性收据；只应用到隔离临时副本。
- 修复后测试：`passed`，退出码 0。
- 原始合成源文件 SHA-256 前后不变；模型进程未启动，原始工作区未写入。
- 完整日志：`reports/interview_demo_runs/interview-m3-20261023T122410Z/m3-demo.log`。

该路径证明的是合成控制流、审批账本和隔离副本测试，不证明模型自主生成补丁、OS 级沙箱或真实用户仓库写入。

## 验证结果

- 演示入口 `-Demo check`：PASS，退出码 0。
- M3 入口：PASS，退出码 0。
- M2 入口：按预期报告 PARTIAL，退出码 3。
- 相关 M2/M3 确定性专项：17 passed、0 failed、0 skipped，退出码 0。
- 唯一 ID 绑定调整后，M3 相关回归再次执行：9 passed、0 failed、0 skipped，退出码 0。
- Python 编译检查：通过，退出码 0。
- Markdown 相对链接检查：通过，退出码 0。
- `git diff --check`：通过，退出码 0；仅报告 README 换行风格提示。

## 能力状态

| 能力 | 状态 | 依据 |
|---|---|---|
| 只读索引、搜索、可信读取 | VERIFIED | M1 确定性测试 |
| OpenManus 受限只读 ToolCollection | VERIFIED（离线范围） | M1.1/M1.2 报告 |
| 真实 Qwen 只读诊断 | PARTIAL / NOT_VERIFIED | 保存运行完成搜索和读取，但数值质量 `UNVERIFIABLE` |
| M3 合成审批与隔离测试 | VERIFIED（合成控制流） | 本轮日志和 SQLite 快照 |
| 模型自主补丁、真实用户仓库写入 | NOT_VERIFIED | 未运行且明确禁止 |
| OS 级沙箱、失败后自主重规划 | NOT_VERIFIED | 不在本轮范围 |
