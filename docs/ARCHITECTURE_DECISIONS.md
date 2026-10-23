# DevFlow Agent 架构决策

## 当前实现补充（2026-10-23）

设计文档中的“建议文件”仍保留为长期路线。当前已落地的模块是 `devflow_agent/repository.py`、`tools.py`、`openmanus_tools.py`、`openmanus_agent.py`、`real_readonly_agent.py`、`answer_quality.py`、`diagnostics.py` 和 `patches.py`。其中 `diagnostics.py` 只消费已捕获 pytest 日志，并复用仓库搜索与可信读取工具；它不会运行日志中的命令，也不会注册写入工具。`patches.py` 是不注册到 Agent 的 diff-only 控制面，只在显式独立审批后应用到不同的合成副本。M1.5 的质量校验和 M2 的固定五案例评测已有确定性证据；M2 真实模型最终输出曾因 3072 context 耗尽而截断，后续唯一尝试被资源预检阻塞，故 M2 仍为 PARTIAL。

> 状态：建议方案。M0 于 2026-10-23 执行，但因无法连接 github.com:443 而未检出源码，M0 未验收；DevFlow 业务模块、依赖锁和基线测试仍未实现。
> 源码基线：FoundationAgents/OpenManus main @ 3309bf4e416fb1c74b008f3e86494439a31bad53（固定 SHA，不追随后续 main）。

## 1. 决策摘要

DevFlow Agent 采用 Python 单进程 CLI 作为首个可用界面，复用 OpenManus 的模型客户端、ReAct/tool-call Agent Loop 与工具 schema。编排、证据模型、仓库边界、补丁审批和测试结果由 DevFlow 自己管理。

首个可演示 MVP 的范围是 M0-M3：在本地合成 Python 仓库上回答只读问题、诊断真实 pytest 失败日志、生成可审查补丁，在明确人工批准后仅修改隔离副本，并在无网络的受限容器中重新测试。最多一次修复尝试和一次有限重规划。此范围暂不含 React 前端、GitHub Issue/PR 在线写入、向量数据库、多 Agent 并行或自动推送。

选择 OpenManus 的理由是它已经提供 Pydantic 消息结构、Agent Loop、工具调用链、模型端点配置和 MCP 接口，适合作为执行内核。选择边界是保留接口，逐步替换开放式工具授权和不适合作为可靠任务状态机的部分。源码评估见 [OpenManus 差距分析](OPENMANUS_FIT_GAP_ANALYSIS.md)。

## 2. 模块边界

建议新增模块放在仓库根目录的 devflow_agent/，与 OpenManus 的 app/ 并列。以下路径是未来建议，不是本轮创建的文件。

| 模块 | 建议文件 | 职责 |
|---|---|---|
| 任务合同 | devflow_agent/contracts.py | Pydantic TaskRequest、EvidenceRef、DiagnosticReport、PatchProposal、RunEvent 等严格数据模型 |
| 编排和预算 | devflow_agent/orchestration/workflow.py、budget.py | 显式状态机、模型调用预算、测试次数、超时和有限重规划 |
| 仓库只读层 | devflow_agent/repository/snapshot.py、indexer.py、search.py、evidence.py | 建立快照、检索文件和代码证据、验证路径/行号/内容 hash |
| Agent 角色 | devflow_agent/agents/repo_qa.py、ci_diagnoser.py、repair_worker.py、reviewer.py | 各角色只返回有 schema 的产物；Agent 不直接决定权限 |
| 安全与执行 | devflow_agent/safety/policy.py、patch_preview.py、workspace.py、sandbox_runner.py | 路径白名单、差异预览、审批状态、隔离测试执行和清理 |
| 审计与恢复 | devflow_agent/persistence/event_store.py、session_store.py | M5 加入 SQLite 事件记录、状态快照和恢复 |
| API/界面 | devflow_agent/api/app.py、routes/jobs.py、routes/events.py | M5 再加入 FastAPI 与 SSE；CLI 是 MVP 的首个交互方式 |
| 评测 | tests/fixtures/repos/、evals/cases/、evals/runner.py | 固定故障仓库、金标证据、基线和消融结果 |

### 数据流

~~~mermaid
flowchart LR
    U[CLI 用户请求] --> O[Workflow 编排与预算]
    O --> R[只读仓库快照与索引]
    R --> A[Repository QA / CI Diagnoser]
    A --> E[EvidenceRef 与诊断报告]
    E --> P[PatchProposal 与差异预览]
    P --> H{人工批准}
    H -->|拒绝| X[取消并记审计]
    H -->|批准| W[隔离工作副本]
    W --> S[受限容器运行测试]
    S -->|通过| D[完成报告]
    S -->|失败且剩余一次预算| O
    O --> L[事件记录与最终报告]
    X --> L
    D --> L
~~~

## 3. 关键设计选择

### 3.1 以单 Agent 工作流为基线，多 Agent 后置

OpenManus PlanningFlow 已能逐个计划步骤选择 executor，但按顺序运行，也没有并发结果合并。MVP 使用显式状态机而不是增加 LangGraph。这样先让每一步的输入、输出、权限、费用和完成条件都可测试。M4 才对比并行调查是否提升定位成功率。

### 3.2 证据先于回答

索引第一版采用结构化文件清单、精确路径/符号匹配和词项检索的组合；结果携带仓库快照 hash、相对路径、行范围、来源内容 hash 和匹配理由。Agent 报告中的 EvidenceRef 必须能在同一快照中重新读取并核对。

不因为“使用 RAG”就引入向量库。先用固定仓库问答和 CI 故障集测 top-k 找回率、引用正确率和定位质量。只有词项/代码结构检索在金标案例上有可量化的遗漏，再评估 embeddings 与向量索引。向量检索增加依赖、模型调用、数据传输和索引更新复杂度；它必须用对照结果证明价值。

### 3.3 任务结果由确定性代码判定

Agent 可以提出计划、证据和补丁，不可以自行宣布测试通过。测试完成条件由进程退出码、超时、JUnit/pytest 输出和基线对比判定。诊断/补丁使用 Pydantic schema 校验；证据路径必须在仓库内，行号必须有效，补丁必须能解析并通过差异约束。

状态建议：CREATED → SNAPSHOTTED → DIAGNOSED → PATCH_PREVIEWED → AWAITING_APPROVAL → APPLYING → TESTING → SUCCEEDED / FAILED / CANCELLED。失败后仅在剩余预算内重新诊断一次；超出预算即停止并输出证据。

### 3.4 安全边界独立于 Agent Prompt

- 默认只读：M1/M2 工具只读仓库快照与测试日志，不注册 Bash、PythonExecute、StrReplaceEditor 等写入/命令工具。
- 仓库根限制：规范化路径、解析 symlink、拒绝逃逸、隐藏凭据、.env、私钥、大型二进制和生成目录；所有文件打开都要再次检查根目录。
- 补丁预览：模型只产出 diff。用户批准的是一个固定 patch hash 和目标快照 hash；任何内容变化都要求重新审批。
- 最小应用：审批后才把 diff 应用到临时副本；允许修改的路径和文件数量由策略限定；原始仓库只读。
- 隔离测试：每次运行创建独立容器和副本；非 root、非 privileged、丢弃 capabilities、只挂载工作副本、默认禁网、资源/进程/磁盘/时限约束；超时后清理容器与整个进程树。上游容器代码的 privileged/root 设置不复用。
- 审计：记录任务 ID、快照 hash、Agent/model/prompt 版本、证据引用、工具调用、审批人/时间/patch hash、测试命令与退出码、费用估算、失败/重规划原因。日志需脱敏，不存 API key。
- 外部内容：Issue、CI 日志、README、MCP server instructions 都当作不可信数据，不赋予其修改策略的权力。

### 3.5 配置、模型与成本

M0 只选一个可用的 OpenAI-compatible API 配置来验证工具调用；不下载权重，不同时接入多个模型供应商。后续可沿用 OpenManus 的 LLM 门面，再用一个窄接口把具体调用与任务费用记录分开。

每个 RunBudget 包括 max_llm_calls、max_input_tokens、max_output_tokens、max_wall_time、max_test_runs 和 max_replans。默认上限应保守，超限后返回状态，不自动续跑。估算方式是每次调用的输入/输出 token 分别乘以所选模型的实时单价，再加上外部服务和容器运行成本。价格随模型变化；确定模型后查阅 [OpenAI API pricing](https://developers.openai.com/api/docs/pricing?content_language=English) 或 [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)，并在评测报告注明查询日期。当前没有选定模型，因此不提供固定美元预算。

### 3.6 存储与交互

M0-M3 不需要服务端数据库。M3 可写追加式 JSONL 审计记录。M5 才加入 SQLite 会话与事件表，先用 Python 标准库 sqlite3；当迁移、并发写入或复杂查询确实需要时再考虑 SQLAlchemy。M5 才评估 FastAPI/SSE 和 React/Next.js。前端不能代替审批状态机或服务端路径校验。

## 4. 技术选型与替代成本

| 选项 | 当前决定 | 取舍 |
|---|---|---|
| Python 3.12 + Pydantic | 采用 | OpenManus README 与 setup.py 已使用；Pydantic 适合显式任务合同。 |
| OpenManus Agent/LLM/tool-call | 选择性复用 | 避免重写基础循环和 provider 调用；复用前修正生命周期、可选依赖和权限边界。 |
| LangGraph | 暂不加入 | 增加另一套状态和检查点抽象；MVP 状态机边界很小，现阶段难以证明其增益。若 M4/M5 出现复杂分支或人类暂停恢复，再做小型对照。 |
| 代码检索 | 精确路径/标识符 + 词项/结构检索 | 不需要向量服务和额外模型；以金标 top-k 与引用准确性决定是否升级。 |
| FastAPI + SSE | M5 再加入 | 初版用 CLI 输出运行事件，避免 Web 服务生命周期和认证扩大 M0/M3 范围。 |
| SQLite / SQLAlchemy | M5 先 SQLite 标准库 | 单机个人项目数据量有限；SQLAlchemy 可在迁移/并发需要后增加。 |
| MCP | 可选扩展 | 源码已有客户端；只启用明确配置的只读 server 和工具。MVP 核心测试不依赖 MCP。 |
| Docker | M3 必须验证；重做安全配置 | 上游已有生命周期和资源限制，但 privileged/root 和默认关闭沙箱不满足安全修复场景。 |
| React/Next.js | M5 视演示价值决定 | 用户要求有可演示界面最终可选；前端排在核心闭环之后。 |
| 本地大模型 | 暂不下载 | 本轮和 M0 都不下载权重；使用已有端点或按需付费 API，记录真实成本。 |

## 5. 新模块最小集合

MVP 实现顺序建议为：

1. devflow_agent/contracts.py：请求、证据、诊断、补丁、测试结果的 schema。
2. devflow_agent/repository/indexer.py、search.py、evidence.py：只读索引与可核对引用。
3. devflow_agent/orchestration/workflow.py、budget.py：有限状态机与硬预算。
4. devflow_agent/agents/repo_qa.py、ci_diagnoser.py：先只读回答和 CI 诊断。
5. devflow_agent/safety/policy.py、patch_preview.py、sandbox_runner.py：审批前不写入，审批后只改隔离副本。
6. devflow_agent/audit.py：JSONL 记录并可由测试重放。
7. M5 再增加 persistence、API 和 UI 模块。

MVP 的合成仓库放在 tests/fixtures/repos/broken_python_project/；评测案例放在 evals/cases/。这些路径当前尚不存在。

## 6. M0 执行记录（2026-10-23）

### 已确认

- 执行前工作区只有四份规划文档；没有既有 Git 仓库、虚拟环境或其他项目文件。当前已在工作区根目录初始化 Git，初始分支名为 `devflow/m0`，登记 `upstream`。没有提交，也没有执行 push。
- 官方 GitHub 只读接口确认固定提交 `3309bf4e416fb1c74b008f3e86494439a31bad53` 存在，提交日期为 2026-08-16，提交说明为 `fix(deps): add missing structlog and cap pillow below 11`。该提交树有 178 个条目，根目录不含 `docs/` 和 `plans/`，现有规划文档路径无冲突。
- 固定提交的 `LICENSE` 为 MIT。此结论不代表传递依赖许可证已经完整审查；依赖未解析，第三方许可证清单尚未生成。
- 本机 Python 为 3.12.7。项目内 `.venv` 已创建，解释器为 3.12.7，pip 为 24.2；没有安装 OpenManus 或 DevFlow 运行依赖。原始 Anaconda 环境未修改。
- 本机没有 `uv`、Docker 或 `py` 启动器；现有 Python 环境可发现 pytest，但本次没有用它运行项目测试。

### 阻塞与影响

- `git fetch --depth=1 upstream 3309bf4e416fb1c74b008f3e86494439a31bad53` 退出码为 1，Git 报告无法连接 `github.com:443`。`.git/FETCH_HEAD` 存在但大小为 0，不能用于检出；当前 `devflow/m0` 是 unborn branch，`HEAD` 尚无提交。
- 因源码未落入工作区，不能对本地源码导入图做验证，不能安全地确定核心依赖、生成真实解析的锁文件、修改上游代码或运行 CLI/测试。不会用 API 文件片段或其他分支拼出一个看似固定的源码副本。
- 配置优先级、密钥脱敏、可选导入和上游接口兼容性尚未改动或验证；没有创建依赖清单、锁文件或测试结果。
- 下一步只应恢复固定 SHA 的 Git fetch，然后核对 `HEAD` 和工作树，再按本文件第 2 节与 M0 路线逐项实施。M1 尚未开始。

### 安全与成本记录

- 没有读取或设置真实 API Key，没有请求模型服务，没有产生模型调用费用；没有读取真实用户仓库。
- 没有运行 OpenManus 的 Bash、PythonExecute、写文件、浏览器或 Docker 工具；没有启用 Docker。
- `python -m venv .venv` 的首次执行因系统临时目录权限而退出 1。随后使用工作区内临时目录引导 pip，并由 `.venv` 的 `pip --version` 命令确认 pip 可用。临时目录已从工作区移除；系统临时目录中的清理警告未尝试越权处理。

## 7. M4 决策：组合现有控制面，不引入第二套 Runtime

M4 落地为 `devflow_agent/repair_workflow.py` 的单运行状态机。它复用 `diagnostics.py`、`patches.py`、`patch_ledger.py` 和 `run_isolated_pytest`，通过 `run_id`、`task_id`、proposal/patch SHA-256、SQLite 事件及脱敏 JSON 报告把诊断、审批和验证绑定起来。审批前只生成 Diff；批准后只向新的合成副本应用一次，源工作区必须保持哈希不变。`RepairWorkflow.replay_report` 以只读方式核验事件序列、状态映射、审批、哈希、测试收据、证据和清理证明，并明确返回 `write_replayed=false`；篡改或不完整的 VERIFIED 报告会被拒绝。

这一决策保持 M1–M3 的工具白名单和 fail-closed 语义，避免为 M4 引入多 Agent、向量库、Docker 或新的持久化系统。合成演示控制流已验证；模型自主补丁、真实用户仓库写入、OS 级沙箱和失败后自主重规划仍未验证。
