# DevFlow Agent 开发路线图

> 本路线图区分静态审查、确定性测试和真实模型验收。当前固定源码为 OpenManus 3309bf4e416fb1c74b008f3e86494439a31bad53，本地分支 devflow/m0-openmanus。此前首次执行时 fetch 阻塞的记录保留在 [M0 验收记录](../docs/M0_ACCEPTANCE.md) 中；当前源码已存在，但 M0 整体验收仍未通过。

## 当前执行状态（2026-10-23）

| 项目 | 状态 | 证据/说明 |
|---|---|---|
| 固定基线 | PASS（只读核对） | 当前分支 devflow/m0-openmanus，HEAD 精确等于固定 SHA。 |
| 源码结构与调用链审查 | PASS（静态） | main.py、run_flow.py、app/、config/、protocol/、tests/ 已检查；详见 [源码审查](../docs/OPENMANUS_SOURCE_AUDIT.md)。 |
| Git 工作区状态 | PASS（只读核对） | 经授权读取可正常运行 `git status`；分支/HEAD 正确。本轮已有的源码改动、未跟踪用户目录均可见；没有暂存、提交或推送。 |
| .venv | PARTIAL | Python 3.12.7；为真实 Agent 导入安装了最小依赖集，未安装完整 requirements 或生成锁文件。 |
| 依赖最小集、锁文件、许可证清单 | PARTIAL | 已安装真实 Agent 导入链所需 8 个直接包，版本见 [M1.2 报告](../docs/M1_2_OFFLINE_AGENT_LOOP.md)；未解析整份 requirements、生成锁文件或扫描完整第三方许可证。 |
| 配置卫生与可选依赖隔离 | PARTIAL | Daytona 缺省配置阻断导入已修复；Agent/Tool 包出口改为懒加载并通过只读入口验证；完整配置卫生仍未评估。 |
| M1 只读专项测试 | PARTIAL | 当前 22 run / 21 passed / 1 skipped；OS 真实目录链接创建受限，其他确定性范围通过。上游全量测试未运行。 |
| 独立只读 CLI | PASS（确定性范围） | help/index/search/read/error 子进程测试在合成临时副本运行；OpenManus 主 CLI、模型和外部服务均 NOT_RUN。 |
| M0 整体验收 | BLOCKED | 需要先完成最小依赖/配置边界、离线契约测试和 CLI 动态检查；写入安全边界也尚未建立。 |
| M1 | PARTIAL | 新增离线只读仓库 CLI、AST 符号索引、搜索、可信读取、OpenManus 离线 Loop 和 loopback-only 真实模型入口。完整真实模型问答未验证；本次模型自主工具选择部分通过，见 [纵向切片验收](../docs/READ_ONLY_REPOSITORY_CLI.md)、[M1.2 报告](../docs/M1_2_OFFLINE_AGENT_LOOP.md) 与 [M1.3 报告](../docs/M1_3_REAL_MODEL_READONLY_AGENT.md)。 |
| M1.1 OpenManus 只读 ToolCollection | READONLY_TOOL_COLLECTION_VERIFIED（集合边界） | 固定 `app/tool/tool_collection.py` 的真实类在隔离导入 seam 下完成只读注册与执行；19 项合计 18 passed、1 skipped。标准 `app.tool` 包导入和 Agent Loop 未运行，见 [M1.1 报告](../docs/M1_1_OPENMANUS_TOOL_COLLECTION.md)。 |
| M1.2 OpenManus 离线 Agent Loop | OFFLINE_AGENT_LOOP_VERIFIED（离线范围） | 正常包导入真实 `ToolCallAgent` / `ToolCollection`；确定性响应完成搜索→可信读取→引用回答。22 项总计 21 passed、1 skipped；真实模型/API 未运行，见 [M1.2 报告](../docs/M1_2_OFFLINE_AGENT_LOOP.md)。 |
| M1.3 真实模型驱动的只读 Agent | PARTIAL；真实门禁 FAIL | Qwen3.5-4B 本地服务与原生工具调用实际运行；模型先搜索、再选中读取，但请求行 1–20 超出 10 行文件，Agent 安全停止，无读取证据和最终答案；38 项确定性测试 37 passed、1 skipped。详见 [M1.3 报告](../docs/M1_3_REAL_MODEL_READONLY_AGENT.md) 与 [运行 JSON](../reports/public/M1_3_REAL_MODEL_ACCEPTANCE_2026-10-23.json)。|
| M1.4 可靠只读工具调用与错误恢复 | PARTIAL；确定性 PASS、真实问答 FAIL | 46 项运行、45 通过、1 跳过；一次真实 Qwen 完成搜索与读取，但回答算错一个负数示例，见 [M1.4 报告](../reports/public/M1_4_READONLY_TOOL_RECOVERY_2026-10-23.md)。|
| M1.5 答案质量门禁与固定评测 | PARTIAL；原始真实运行 FAIL，离线重评 PASS | 原始 run `20261023T102601Z-m15-python-health` 保留退出码 3；修复 Markdown 引用和零声明数值语义后，使用同一回答/工具轨迹离线重评为 PASS（引用、-250、SHA-256 均通过），未重新启动模型；详见 [M1.5 报告](../reports/public/M1_5_ANSWER_QUALITY_2026-10-23.md)。|

## M0 — 固定基线与源码安全审查（静态交付完成，阶段未通过）

### 已完成（静态证据）

- 固定 OpenManus 提交已在目标分支 checkout；没有改变 upstream、提交或推送。
- 完成 CLI、Agent Loop、Tool Registry、LLM 配置、PlanningFlow、MCP、Docker/Daytona 工具链的源码审查。
- 记录了默认本机执行风险、路径边界缺失、Docker 配置、审批/持久化缺口和依赖声明差异。
- 已更新 [源码审查](../docs/OPENMANUS_SOURCE_AUDIT.md) 与 [M0 验收记录](../docs/M0_ACCEPTANCE.md)。

### 仍需完成的 M0 门禁

1. 修正只读入口的 eager imports 与依赖分层；只把经过源码导入和安装验证的包放入 core。
2. 建立配置密钥卫生规则：本地配置不进版本库，模型凭证从运行环境安全注入；默认关闭非必要 MCP 启动。
3. 添加不需要模型、Docker 或网络的离线配置、消息/工具协议和 CLI 契约测试。
4. 依赖解析后生成可复现锁文件与第三方许可证清单。
5. 在目标 Windows 环境执行只读 CLI 帮助/启动自检，并记录真实退出码。

**M0 通过标准：** 固定 SHA 不变；最小离线环境可安装；配置不暴露密钥；OpenManus 只读 CLI 和契约测试通过；不启动任意宿主机写入或命令执行。OpenManus 主 CLI/契约测试仍为 NOT_RUN；独立 M1 只读仓库 CLI 的实际结果见上方状态表。个人补丁应用在 M0/M1 阶段保持关闭。
## M1 — 只读代码仓库问答与证据

### 交付物

- devflow_agent/contracts.py：TaskRequest、EvidenceRef、RepoAnswer。
- devflow_agent/repository/indexer.py：文件清单、忽略规则、相对路径、内容 hash；不跟随逃出根目录的 symlink。
- devflow_agent/repository/search.py：路径/符号/标识符和词项检索；先不接向量库。
- devflow_agent/repository/evidence.py：生成及重新核验 path、行范围、快照 hash、片段 hash。
- devflow_agent/agents/repo_qa.py：只读工具，不注册 Bash/PythonExecute/StrReplaceEditor 写入命令。
- tests/fixtures/repos/tiny_python_repo/ 和 tests/unit/test_repository_index.py、test_evidence_refs.py。
- evals/cases/repo_qa.jsonl 建立第一批固定问答；记录引用命中率和答案正确性。

### 验收与环境

无需 Docker、GitHub、数据库或真实模型即可运行单测。使用模型的端到端演示需要一个 OpenAI-compatible API 端点。每个事实性回答必须附有能在仓库快照复核的路径与行范围；不存在证据时必须返回 UNKNOWN 或请求澄清。测试覆盖忽略 .git、venv、凭据文件、二进制、symlink/path traversal。

命令建议：

    .\.venv\Scripts\python.exe -m pytest -q tests/unit/test_repository_index.py tests/unit/test_evidence_refs.py
    .\.venv\Scripts\python.exe -m devflow_agent.cli ask --repo tests/fixtures/repos/tiny_python_repo --question "..."

风险：Windows 大小写和路径分隔符；编码/超大文件；日志与仓库内容中的 prompt injection；检索结果看似相关但无法支持答案。通过引用校验和错误案例测试降低风险。

### 当前已实现的确定性只读切片（2026-10-23）

- 模块：devflow_agent/repository.py、devflow_agent/cli.py、devflow_agent/tools.py、devflow_agent/openmanus_tools.py。
- CLI：python -m devflow_agent.cli --help；index、search、read 都要求显式 --repo。
- 安全：路径根限制、符号链接/junction 逃逸检查、敏感目录和文件排除、UTF-8 文本 allowlist、单文件/扫描/总字节/输出限制、实际磁盘字节 SHA-256、读取前后版本复核。
- 搜索记录明确标为候选而非模型读取证据。只读 Registry adapter 仅接受空工具集合。
- 测试：.venv Python 3.12.7 下执行 unittest discover -s tests/devflow_agent -v，M1.1 后 19 run / 18 passed / 1 skipped。OS link creation 受限；解析逃逸分支由确定性模拟测试覆盖。CLI 与真实 ToolCollection 源码测试使用临时合成项目并核对内容哈希未变化。
- M1.1：真实固定版本 ToolCollection 类经隔离导入 seam 动态加载，两个只读工具经其真实 execute() 调用；见 [M1.1 报告](../docs/M1_1_OPENMANUS_TOOL_COLLECTION.md)。
- M1.2：修复 Daytona 缺省配置导入错误和 Agent/Tool eager import；正常包导入真实 ToolCallAgent/ToolCollection，并以真实框架 Loop 执行确定性搜索、读取、错误拒绝和最终回答。没有调用模型/API；完整依赖与许可证仍未锁定。
- M1.3：新增的 loopback-only `ask` CLI 已使用本机 Qwen3.5-4B 实际发送两次模型请求并形成搜索→读取工具调用；读取范围越界后 fail-stop。真实服务层通过，完整只读 Agent 问答未通过，保持 PARTIAL；38 项确定性测试 37 passed、1 skipped。默认拒绝云端 endpoint。
- 仍未完成：读取成功后将真实内容传回模型继续推理、最终回答和真实引用准确性评测、索引/Agent 状态持久化、写入审批和修复闭环；本机现有 llama.cpp 运行时均没有 GPU backend。


## M1.4 — 可靠的只读工具调用与有限错误恢复（2026-10-23）

状态：PARTIAL。确定性门禁通过：46 项运行、45 通过、1 跳过。搜索结果携带实际行数；可信读取允许有界默认预览，显式非法范围仍拒绝；OpenManus 原生工具结果按调用 ID 配对；每次 ask 对读取范围错误最多恢复一次，并在预算耗尽时明确失败。错误路径恢复由真实 OpenManus Agent Loop 的确定性响应测试覆盖。

本地 Qwen3.5-4B 确实完成搜索、两次可信读取并输出最终答案，但真实运行退出码为 3。模型引用 src/billing.py lines 3-7 与真实读取范围相符；引用解析器现已兼容该格式并有回归测试。模型同时把 sum([-100, 50, -200]) 算成 -350，正确值为 -250，所以真实回答验收仍失败。没有重试模型，M1.4 不得标成真实模型已验证。详见 [M1.4 报告](../reports/public/M1_4_READONLY_TOOL_RECOVERY_2026-10-23.md)、[机器可读报告](../reports/public/M1_4_READONLY_TOOL_RECOVERY_2026-10-23.json) 和 [运行轨迹](../reports/public/M1_4_READONLY_TOOL_RECOVERY_2026-10-23.json)。
## M1.5 — 答案质量门禁与固定评测（2026-10-23）

状态：PARTIAL。受限 AST、可信读取来源和有界纠正已有确定性测试；历史完整回归 58 项运行、57 通过、1 跳过。本次质量专项 16 项通过。运行 20261023T102601Z-m15-python-health 已真实完成 /health HTTP 200、模型身份检查、3 次 Qwen 请求和搜索→可信读取→最终回答，合成文件未改变，自有进程清理通过。

真实答案计算结果 -250 正确，但现有引用解析器不接受路径外的 Markdown 反引号；表达式与结果分段展示也使数值抽取为 0。最终质量门禁 MISSING_EVIDENCE、CLI 退出码 3，真实验收 FAIL。不得把原始 numeric PASS（零条算式）当成数值已验证。详见 [M1.5 综合报告](../reports/public/M1_5_ANSWER_QUALITY_2026-10-23.md)。

离线修复已支持 Markdown 引用边界和分段算式，并在零提取时返回 UNVERIFIABLE；`scripts/recheck_m15_saved_answer.py` 对同一保存回答完成 PASS（不是新的真实模型运行）。真实运行历史仍保留原始退出码 3，因此总体继续 PARTIAL；不得重写原始记录。

M2 已实现最小只读诊断切片：`devflow_agent/diagnostics.py` 解析有界 pytest 日志，复用 `repository_search_readonly` 与 `repository_readonly_read`，输出失败测试、异常、候选根因、路径/行号/摘要哈希和不确定性；`devflow_agent.cli diagnose` 支持 JSON/Markdown，并可用 `--compact` 输出最多三条证据的最终诊断契约。五案例固定评测位于 `evals/m2_ci_failures.jsonl`，确定性测试和 CLI 演示通过。最新唯一真实 Qwen3.5-4B 运行 `20261023T-m2-qwen35-4b-r6-ctx4096` 已通过服务健康、模型身份、搜索、两次可信读取、工具调用配对和未截断最终回答；引用门禁通过，合成文件未改变，进程清理通过。由于诊断中的项目函数调用 `total([1, -1])` 超出受限数值校验器的可验证范围，数值门禁为 `UNVERIFIABLE`，外层验收退出码为 1，M2 整体继续 PARTIAL。证据见 [M2 报告](../reports/public/M2_CI_DIAGNOSTICS_2026-10-23.md) 和 [最新运行 JSON](../reports/public/M2_CI_DIAGNOSTICS_2026-10-23.json)。

## M2 — CI Debug 诊断

### 交付物

- `devflow_agent/diagnostics.py` 中的 `FailureEvent`、`DiagnosticReport`、有界 pytest 日志解析和证据关联。
- 接收已捕获的 pytest 日志和退出码；提取失败测试、异常类型、断言消息、源码位置；复用 M1 搜索与可信读取工具。
- `evals/m2_ci_failures.jsonl`：五个固定日志案例、合成仓库快照及预期定位/分类。
- `devflow_agent.cli diagnose` 只读演示命令；不调用 GitHub API，不执行任意日志内命令。

### 验收与环境

合成项目先生成一次真实失败测试日志，再输入 DevFlow；报告要给出日志片段和源码证据链接。量化 top-1/top-3 文件定位率、测试定位率、根因分类准确率、证据引用精度；与只看日志的基线比较。无模型单测用假客户端，现场演示可选真实 API。

风险：日志可能包含用户秘密或恶意提示；要求脱敏，日志里的命令不能自动执行；测试堆栈行号容易随代码变化，案例需固定仓库 hash。


M2 数值适用性离线改进（20261023T-m2-qwen35-4b-r6-ctx4096 之后）：`answer_quality.py` 现在区分 `SUPPORTED` 与受限的 `STATICALLY_VERIFIABLE` 项目函数调用；只接受可信读取、单一无副作用返回表达式，分支、循环、动态调用和外部状态继续 `UNVERIFIABLE`。新增 8 项回归全部通过；保存的真实回答离线重评引用 PASS、数值仍 `UNVERIFIABLE`，原始真实运行未被改写，也没有再次启动模型。

## M1–M3 面试演示交付冻结（2026-10-23）

功能范围在 M1–M3 冻结。`scripts/run_interview_demo.ps1` 提供 `check`、`m2` 和 `m3` 三个有界入口：M2 只展示保存的真实 Qwen 轨迹并生成新的离线摘要，M3 运行合成仓库的诊断→Diff→独立审批→隔离测试控制流。入口不会自动启动模型、调用外部 API、写入真实工作区或覆盖历史证据。M2 真实质量状态仍为 `PARTIAL / NOT_VERIFIED`；M3 合成控制流保持已验证范围，模型自主补丁、真实用户仓库写入、OS 级沙箱和失败后自主重规划仍未验证。面试说明见 [`docs/INTERVIEW_OUTLINE.md`](../docs/INTERVIEW_OUTLINE.md)。

## M3 — 受控补丁与测试闭环（MVP 完成点）

### 当前实现切片（2026-10-23）

`devflow_agent/patches.py` 已提供不注册到 Agent ToolCollection 的最小补丁控制面：从可信文件读取创建 `PENDING_APPROVAL` unified diff，绑定 `task_id`、`run_id`、workspace、规范化相对路径、原始 SHA-256、替换内容 SHA-256 和 patch SHA-256；独立 reviewer 决策后只允许一次性应用到不同的隔离副本。`devflow_agent/patch_ledger.py` 现在以 SQLite 持久化 proposal、审批决定、`APPLYING/APPLIED/RESULT_UNKNOWN` 状态事件、一次性收据及有界测试结果；`run_isolated_pytest` 只运行固定 `pytest -q`，记录退出码、截断标记和超时清理。拒绝、源文件版本变化、路径越界、审批绑定不匹配、重复消费和把源工作区作为目标均 fail-closed。当前仍不是生产写入工具，也不声称具备 OS 级沙箱或模型自主补丁能力。

确定性 `tests/devflow_agent/test_m3_patch_flow.py` 为 5/5，`tests/devflow_agent/test_m3_ledger.py` 为 4/4 通过；`scripts/demo_m3_repair.py --approve` 在临时合成仓库中实际经历基线 pytest 失败、只读诊断、Diff 预览、SQLite 账本记录、独立合成 reviewer 审批、隔离副本写入和修复后 pytest 通过，源副本 SHA-256 未变化。该演示证明安全控制流和持久化证据，不证明模型自主生成补丁或 OS 级沙箱。

### 规划中的后续交付物

- devflow_agent/safety/policy.py：仓库根目录约束、补丁白名单、文件数/大小限制。
- devflow_agent/safety/patch_preview.py：统一 diff、目标文件和 patch hash；审批前不落盘。
- devflow_agent/safety/sandbox_runner.py：独立副本、非 privileged 容器、非 root、禁网、资源/时间限制、容器/子进程清理。
- devflow_agent/orchestration/workflow.py：先诊断再提出补丁；命令行显示 diff；用户明确 approve 后才应用到隔离副本；跑失败用例和回归测试；至多 1 次修复与 1 次重规划。
- tests/fixtures/repos/broken_python_project/：一个能稳定从红变绿的合成项目；包含恶意路径、symlink、补丁越界和测试超时案例。
- tests/integration/test_repair_workflow.py：记录拒绝批准、批准应用、测试通过、测试仍失败、有界停止和 cleanup。

### 验收与环境

需要 Python 3.12 和可用 Docker Engine/Desktop；MVP 不需要外网。基础镜像应已在本地，容器安装步骤不联网。若没有 Docker，不以本机 Bash 替代隔离要求。测试通过标准是从初始红色基线到审批后补丁在隔离副本中绿色，原仓库内容 hash 不变，拒绝审批时零文件修改，进程/容器在成功、失败、超时和取消路径都被清理。

命令建议：

    .\.venv\Scripts\python.exe -m pytest -q tests/integration/test_repair_workflow.py
    .\.venv\Scripts\python.exe -m devflow_agent.cli repair --repo tests/fixtures/repos/broken_python_project --log tests/fixtures/logs/broken_python_project.txt --dry-run
    .\.venv\Scripts\python.exe -m devflow_agent.cli repair --repo tests/fixtures/repos/broken_python_project --log tests/fixtures/logs/broken_python_project.txt

最后一条交互式命令必须先显示补丁 hash 和 diff，再等待用户批准。拒绝、取消或超预算都应产生可审计的非成功终态。

风险：容器仍依赖宿主 Docker 安全性；Windows 进程树处理；基础镜像供应链；测试夹具依赖；模型生成不合法或攻击性补丁。测试场景必须覆盖资源超限、路径穿越、symlink 逃逸、网络访问和 cleanup。

## M4 — 有实证价值的多 Agent 协作

### 交付物

- 两个或三个有明确分工的调查 Agent，例如测试日志定位、源码路径分析；并行只用于相互独立的只读调查。
- devflow_agent/orchestration/merge_evidence.py：合并 EvidenceRef、去重、标记分歧，不允许低置信结果覆盖直接测试证据。
- 冲突样例：Agent 得出不同根因、同一文件建议不同补丁、Agent 无证据断言。
- 单 Agent / 并行 Agent 对照评测：成功率、证据精度、token、费用、延迟、错误数；只在指标改善且成本符合预算时保留多 Agent。

### 验收与环境

使用 M1/M2/M3 同一固定集；同一模型版本、温度、预算、prompt 和仓库快照。报告均值/中位数以及样本数；少量案例不宣称统计显著。每个 Agent 输出都要保留身份和证据来源。

风险：成本与延迟乘数、重复请求、上下文分叉和结果冲突。若并行没有可测的净收益，保留单 Agent，不为了 Agent 数量而实现角色堆叠。初版继续使用 asyncio 编排；若状态/恢复逻辑显著复杂，再评估 LangGraph。

## 后续候选 — 运行历史、恢复、实时状态与演示界面

### 交付物

- SQLite event/session store，记录状态迁移、证据、审批、补丁和测试退出结果。
- 重启后从安全检查点恢复，恢复前验证仓库快照与 patch hash 未变化。
- FastAPI 任务 API 与 SSE 状态通道；React/Next.js 仅在终端演示不足以表达证据/审批时加入。
- 可从历史 Run 重放诊断/测试结果，不重新发起模型请求；一键展示评测摘要和消融图表。

### 验收与环境

单机无云数据库；SSE 重连后能补拉 last_event_id 之后的事件；断电/终止模拟后恢复到合法状态；待审批的 Run 恢复后仍停在审批门之前。不得在恢复时重复应用补丁或重复收费调用。

风险：事件顺序一致性、敏感日志、数据库迁移、SSE 断线重放。M1-M4 已累计固定评测数据；M5 整合统计、可复现报告和界面。

## M6 — 可选 GitHub Issue/PR 集成

在 MVP 与安全评测稳定之后再考虑。只读读取 Issue、PR diff、CI 日志；每个输入转换为本地结构化 TaskRequest。权限只申请只读；仍不创建 PR、不推送分支。需要额外 GitHub token、权限审查、速率限制和外部服务成本。验收使用 mock API 与脱敏固定响应，再做明确授权的只读演示。

## 预计运行环境与外部服务

| 阶段 | 运行环境 | 模型/外部服务 |
|---|---|---|
| M0 | Windows + Python 3.12 + 本地 venv；可选 uv | 无需模型即可测；真实 smoke 需一个支持 tool calls 的 API 端点和 key |
| M1 | 本地文件系统与 pytest | 现场答案可选一个 API；不下载权重 |
| M2 | 本地日志、合成仓库 | 可选模型 API；不需要 GitHub |
| M3 | Docker Engine/Desktop、本地 Linux 容器镜像 | 不需要外网；模型用于补丁建议时调用次数受限 |
| M4 | 同 M3 | 单一模型端点；增加 Agent 数不自动扩大预算 |
| M5 | 本地合成仓库、SQLite、pytest | 不需要模型或外部服务；默认一次重规划 |
| M6 | GitHub 只读 API | token、网络与 GitHub 限流；推送/写权限不需要也不应配置 |

## M4 实际交付（2026-10-23）

本轮将原先规划中的多 Agent 对照暂缓，先交付一个可审计的单工作流纵向切片：`devflow_agent/repair_workflow.py` 管理基线测试、只读诊断、可信证据、哈希绑定 Diff、显式审批、SQLite 账本、一次性隔离应用、固定 pytest 验证和脱敏报告。`scripts/demo_m4_workflow.py --approve` 在合成仓库中真实执行了红色基线、诊断、提案、合成独立审批、隔离副本写入和绿色测试；源副本哈希未变化，报告可只读回放。专项测试 12/12 通过；完整确定性回归 104 项通过、1 项因主机不允许创建 junction 跳过。

M4 在上述合成控制流范围内为 `VERIFIED`。独立复核补强了报告回放的一致性校验，并确保隔离副本清理失败不能进入 `VERIFIED`。真实模型自主生成补丁、真实用户仓库写入和 OS 级沙箱/Docker 隔离仍为 `NOT_VERIFIED`。

## M5 实际交付：有界自主重规划（2026-10-23）

`devflow_agent/bounded_replanning.py` 已在 M4 之上实现证据绑定的有限尝试编排。默认 `max_replans=1`，每次新候选必须绑定最新失败指纹、允许路径和源文件 SHA-256，并重新经历 M4 的诊断、显式审批、独立 SQLite 账本、一次性隔离应用和固定 pytest。重复失败、重复候选、缺少证据、越权路径、审批缺失/拒绝、超时、持久化错误及预算耗尽都有明确终态。M5 顶层报告以 SHA-256 绑定各次 M4 报告；只读回放不会重新运行测试或写入。

合成运行 `20261023T-m5-bounded-r1` 实际经历第一次获批候选测试失败、产生新失败证据、第二次独立获批候选测试通过；两个尝试各有一份一次性收据，源合成仓库运行前后哈希一致。该确定性控制流为 `VERIFIED`。真实模型自主生成/修正补丁、真实用户仓库写入、OS 级沙箱、崩溃恢复和并发审批仍为 `NOT_VERIFIED`。详细边界与命令见 [`docs/M5_BOUNDED_REPLANNING.md`](../docs/M5_BOUNDED_REPLANNING.md)。

## 阶段出口条件

每阶段的交付物、测试日志、固定源/仓库 hash 和风险记录都进入本地版本历史。只有验收命令全绿、离线单测不依赖供应商、现场演示没有伪造输出、失败路径被记录，才标记阶段完成。M0 通过后再进入 M1；M1-M3 通过后，首个 DevFlow MVP 才成立。
