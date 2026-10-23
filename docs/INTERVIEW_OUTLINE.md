# DevFlow-Agent 面试讲解提纲

## 60 秒版本

这是一个基于固定版本 OpenManus 改造的本地软件研发 Agent 原型。它先把仓库检索、可信文件读取、pytest 失败诊断和补丁审批拆成可以核验的步骤，再让模型只在明确的只读工具白名单内工作。M1–M2 负责从真实文件和失败日志建立证据，M3 生成绑定文件摘要的候选 Diff，经独立审批后只应用到隔离副本并运行固定测试。

## 面试重点

1. **为什么选择 OpenManus**
   - 复用了固定提交中的 LLM 配置、ToolCollection、ToolCallAgent 和消息/工具调用协议。
   - 在外层增加仓库边界、可信读取证据、诊断报告和补丁审批，而不是重新实现模型循环。

2. **实际实现的模块**
   - `devflow_agent/repository.py`：索引、搜索、路径边界和基于磁盘字节的 SHA-256 读取证据。
   - `devflow_agent/tools.py` / `openmanus_tools.py`：只读工具及受限 ToolCollection 适配。
   - `devflow_agent/diagnostics.py`：有界 pytest 日志解析和证据关联。
   - `devflow_agent/answer_quality.py`：引用、数值适用性和 fail-closed 质量门禁。
   - `devflow_agent/patches.py` / `patch_ledger.py`：Diff 摘要、独立审批、一次性收据和隔离副本测试。

3. **为什么工具调用需要权限边界**
   - 模型输出只是一项请求，服务端仍要验证路径、工作区、文件摘要和允许的工具集合。
   - 当前真实模型入口不注册 Bash、PythonExecute、文件编辑或外部写入工具；候选补丁也不直接接入只读 Agent。

4. **为什么审批记录必须持久化**
   - SQLite 记录任务/运行、补丁摘要、原始文件 SHA-256、审核者、消费状态和测试结果，避免重启后把旧授权误用于新内容。
   - 一次性消费和 `RESULT_UNKNOWN` 规则用于阻止不确定副作用自动重放。

5. **为什么 `UNVERIFIABLE` 不能当作 PASS**
   - 引用存在不等于结论正确；数值声明必须有受限 AST 或可信源码证据支撑。
   - 无法证明函数行为、缺少读取证据或答案被截断时，质量门禁保持失败/不可验证。

6. **当前不足与后续方向**
   - M2 的保存真实 Qwen 运行完成了搜索、可信读取和未截断回答，但项目函数调用的数值门禁仍为 `UNVERIFIABLE`，所以整体保持 `PARTIAL`。
   - M3 通过的是合成控制流；模型自主生成补丁、真实用户仓库写入、OS 级沙箱和失败后自主重规划仍未验证。
   - 下一步应在不放宽安全边界的前提下改善真实模型的结构化诊断输出，再进行一次有界真实验收。

## 面试演示边界

- `scripts/run_interview_demo.ps1 -Demo m2` 只读取并重新展示保存的真实模型证据，不启动模型，也不把离线重评当作新的真实运行。
- `scripts/run_interview_demo.ps1 -Demo m3` 运行合成仓库控制流；补丁和 Reviewer 是确定性的，输出不能描述成模型自主修复。
- 任何未在报告和运行日志中实际执行的能力都标记为 `NOT_VERIFIED`。

## M4：证据驱动修复工作流

M4 是一个有界的单工作流编排器，不是另一套 Agent Runtime。它消费已有 pytest 失败日志，在隔离副本执行基线测试，复用只读诊断和可信源码证据，生成绑定原始文件与补丁摘要的 Diff，并在显式审批前停止。批准后由 SQLite 账本记录一次性授权，只向新的合成副本应用补丁，再运行固定 pytest。报告保留状态事件、测试退出码、审批和源目录哈希，回放只验证证据，不执行写入。

面试演示命令：

```powershell
.\.venv\Scripts\python.exe scripts\demo_m4_workflow.py --approve --run-id m4-demo-interview
```

该演示证明确定性的证据和安全合同，不证明模型自主选择补丁、真实个人仓库写入或 OS 级沙箱；这些能力仍为 `NOT_VERIFIED`。
