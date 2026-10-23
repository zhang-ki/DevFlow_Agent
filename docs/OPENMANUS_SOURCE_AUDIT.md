# OpenManus 固定提交源码审查（M0）

审查日期：2026-10-23
本地仓库：D:/agent/DevFlow-Agent
分支：devflow/m0-openmanus
固定基线：3309bf4e416fb1c74b008f3e86494439a31bad53
范围：固定提交的源码、配置、测试、依赖清单和现有规划文档。此文档记录静态审查，不表示应用已安装、启动或通过动态测试。

## 结论与证据等级

固定提交已在本地 checkout，当前 HEAD 与指定 SHA 完全一致。源码、配置、协议目录、测试和 .venv 均存在。完成了本报告列出的静态调用链与风险审查；本轮没有安装依赖、启动 Agent、调用模型/API 或运行任何测试。git status/git diff 在该工作区报“this operation must be run in a work tree”，因此无法通过常规状态命令确认全部未提交变更；索引查询显示没有暂存变更、没有 Git 跟踪的修改，原先可见的未跟踪文件属于现有 docs/、plans/ 文档。没有提交或推送。

| 事项 | 状态 | 证据边界 |
|---|---|---|
| 指定分支和固定 HEAD | PASS（只读核对） | git branch --show-current、git rev-parse HEAD |
| 入口、Agent、工具、Flow、配置源码审查 | PASS（静态） | 下文列出的真实源文件和类/函数 |
| 安全实现审查 | PASS（静态审查已完成） | 仅能证明代码路径；不是隔离或安全测试通过 |
| 依赖导入与清单比较 | PASS（静态审查已完成） | 未解析安装图、未安装依赖 |
| 测试/CLI/API/Agent 动态验收 | NOT_RUN | 本阶段明确不执行 |
| 个人代码修改安全闭环 | BLOCKED | 当前默认工具可直接执行宿主机代码和写宿主机文件，无审批/隔离补丁合同 |

## M0-1：项目结构、入口和真实调用链

主要目录包括 app/agent/、app/tool/、app/flow/、app/sandbox/、app/daytona/、config/、protocol/、tests/ 和 workspace/。

### CLI 与单 Agent

main.py 的 main() 解析可选 --prompt；随后调用 Manus.create()，读取命令行或控制台输入，调用 agent.run(prompt)，并在 finally 中调用 agent.cleanup()。Manus.create() 建立 Manus 后运行 initialize_mcp_servers()。

主要执行链：

~~~mermaid
flowchart TD
    CLI[main.py: main] --> CREATE[Manus.create]
    CREATE --> MCP[Manus.initialize_mcp_servers]
    MCP --> RUN[BaseAgent.run]
    RUN --> STEP[ReActAgent.step]
    STEP --> THINK[ToolCallAgent.think]
    THINK --> LLM[LLM.ask_tool]
    LLM --> RESPONSE[Provider ChatCompletion message]
    RESPONSE --> ACT[ToolCallAgent.act]
    ACT --> PARSE[ToolCallAgent.execute_tool: JSON parse]
    PARSE --> REG[ToolCollection.execute]
    REG --> IMPL[Registered BaseTool / MCP server]
    IMPL --> RESULT[tool message with tool_call_id]
    RESULT --> RUN
    RUN --> CLEAN[main.py finally: Manus.cleanup]
~~~

- app/agent/base.py 的 BaseAgent.run() 保存初始用户消息，按 current_step < max_steps 循环调用 step()；BaseAgent.state_context() 临时切换状态。基类默认 max_steps=10，Manus 和 SWEAgent 覆盖为 20。
- app/agent/react.py 的 ReActAgent.step() 顺序调用 think()、act()。
- app/agent/toolcall.py 的 ToolCallAgent.think() 把消息、系统提示、available_tools.to_params() 和 tool_choice 传给 LLM.ask_tool()；收到响应后以 Message.from_tool_calls() 保存 assistant 工具调用。act() 逐个执行工具，并以原 tool_call_id 写入工具消息。execute_tool() 对 JSON 参数做 json.loads，随后路由到 ToolCollection。
- app/tool/base.py 的 BaseTool.to_param() 提供工具元数据和参数 Schema；app/tool/tool_collection.py 的 ToolCollection.execute() 通过名称查找并调用工具。执行入口没有统一使用 JSON Schema/Pydantic 对完整参数对象做严格验证；实际约束依赖 Python 参数绑定、工具内检查和错误处理。
- app/llm.py 的 LLM.ask_tool() 使用非流式 Chat Completions 工具请求；计算输入与工具描述 token，检查 max_input_tokens（若配置），验证工具元素至少为 dict 且含 type，然后调用 Provider。LLM 按 config name 缓存实例；api_type 分支选择 Azure、Bedrock 或 OpenAI-compatible client。该方法默认请求 timeout 为 300 秒，并配置最多 6 次 tenacity 尝试；retry_if_exception_type 包含通用 Exception，而 app/exceptions.py 的 TokenLimitExceeded 继承 Exception，与“不要重试 TokenLimitExceeded”的注释不一致。没有在本轮发送请求。
- app/schema.py 的 Message、ToolCall、Memory 是内存消息结构。Memory.add_message() 受 max_messages 限制并删除旧消息；不是会话持久化。
- main.py 运行结束会清理 Manus；BaseAgent.run() 另调用全局 SANDBOX_CLIENT.cleanup()。基类状态上下文在 finally 中恢复旧状态，异常设置的 ERROR 状态随后会恢复为先前状态；清理调用位于主循环之后，若循环异常则可能跳过。以上是源码控制流观察，不代表实际异常时一定残留。

### PlanningFlow

run_flow.py 的 run_flow() 建立 Manus agent，并按 config.run_flow_config.use_data_analysis_agent 选择性加入 DataAnalysis，通过 FlowFactory.create_flow(FlowType.PLANNING, agents) 建立 Flow。调用 asyncio.wait_for(flow.execute(prompt), timeout=3600) 设置整次 Flow 的 3600 秒超时。

app/flow/planning.py 的 PlanningFlow.execute() 用 PlanningTool 创建内存计划，逐步选择执行者并调用 _execute_step()；后者 await executor.run(step_prompt) 正常返回后调用 _mark_step_completed()。因此“Agent 返回”即视为步骤完成，没有独立的 pytest 退出码/验收合同来判定步骤是否达成。Flow/计划由内存对象持有，未发现持久任务恢复存储。

## M0-2：安全边界

以下“代码证实”只说明源码中存在相应机制；“测试覆盖”只说明有测试代码，未运行的测试不算通过。

| 面向 | 实际实现与证据 | 状态/主要风险 |
|---|---|---|
| 默认 CLI 工具 | app/agent/manus.py 的 Manus.available_tools 包含 PythonExecute、StrReplaceEditor、AskHuman、Terminate。 | Python 与文件写入工具默认启用；PERSONAL/只读用途不安全。 |
| PythonExecute | app/tool/python_execute.py 的 PythonExecute.execute() 用 multiprocessing.Process 执行 exec(code, safe_globals, safe_globals)；safe_globals 包含完整 __builtins__。默认超时 5 秒，超时只终止直接 worker 并 join(1)。 | 任意 Python 代码可影响运行 Agent 的主机用户权限；这不是沙箱，也未见后代进程树管理或取消协议。 |
| 文件编辑器 | app/tool/str_replace_editor.py 的 StrReplaceEditor._get_operator() 在 config.sandbox.use_sandbox 为 false 时返回 LocalFileOperator。validate_path() 要求绝对路径并检查存在性/目录类型，不限制到 config.workspace_root。create、str_replace、insert、undo_edit 会调用 write_file。 | 默认可读写该用户有权限访问的任意绝对路径；无统一 diff 预览、仓库边界、审批或持久审计。 |
| Bash | app/tool/bash.py 的 _BashSession.start/run/stop 用 create_subprocess_shell("/bin/bash", shell=True, preexec_fn=os.setsid)，命令作用于宿主环境；工具注册在 SWEAgent，不在默认 Manus 工具列表。 | 任意 shell 有宿主机副作用；代码依赖 POSIX /bin/bash 和 setsid，Windows 原生兼容性 NOT_VERIFIED。超时路径设置标记并抛错；未见自动清理整棵进程树。 |
| 工具参数校验 | ToolCallAgent.execute_tool() 解析 JSON 后直接 ToolCollection.execute()；工具 Schema 作为 Provider 参数暴露。 | 未发现统一 schema 严格校验入口；未知工具名会拒绝，但完整参数约束依赖单个工具实现。 |
| Docker sandbox 默认 | app/config.py 的 SandboxSettings 设置 use_sandbox=False、network_enabled=False、memory_limit=512m、cpu_limit=1.0、timeout=300。DockerSandbox.create() 设置容器 CPU/内存、网络模式，并绑定工作目录。工作目录与自定义 host mount 均为 rw。 | 默认 Agent 不使用 Docker sandbox；开启后也不是只读工作区。 |
| Docker 命令特权与路径 | app/sandbox/core/terminal.py 的 DockerSession.create() 将 exec 配置为 privileged=True, user=root。DockerSandbox._safe_resolve_path() 只拒绝路径组件 ..，绝对路径不限制于工作目录。 | 容器命令权限偏高；host RW 挂载和工具命令增加风险。Docker daemon/宿主配置影响不在代码内被证明。 |
| Docker 清理 | DockerSandbox.cleanup() 尝试关 terminal、停止容器并 force remove；SandboxManager.cleanup() 等待最多 30 秒后清空跟踪引用。_safe_delete_sandbox() 捕获错误并记录日志。 | 有资源清理代码，不等于后代/异常路径已验证；管理器可能在等待结束后清引用，清理错误不一定向任务调用者 fail-closed。 |
| MCP | app/agent/manus.py 的 initialize_mcp_servers() 默认尝试启动 uvx browser-use --cli-mcp，除非 Browser Use 已配置或 OPENMANUS_DISABLE_BROWSER_USE 为真值。另从 config/mcp.json 读取 stdio/SSE 服务。app/tool/mcp.py 的 MCPClients 注册远端 schema 并转发 call_tool。 | stdio 服务以当前用户启动；MCP server 可赋予任意自身能力。源码没有统一 per-tool 权限 allow-list、独立审批或持久审计。 |
| Daytona | app/agent/sandbox_agent.py 的 SandboxManus.create() 会初始化 MCP 和远端 sandbox。app/daytona/sandbox.py 的 create_sandbox() 使用 public=True 并配置浏览器服务；DaytonaSettings.VNC_password 默认 123456。 | 使用该 Agent 会触发云端资源和外部服务；公开预览/默认口令对个人安全默认值不可接受。未调用 Daytona API。 |
| Browser | BrowserSettings.disable_security 默认 true（app/config.py）；BrowserAgent 使用 MCP 浏览器会话。 | 浏览器安全限制默认配置值得收紧；未运行浏览器。 |
| 本地凭证配置 | Config._get_config_path() 优先加载 config/config.toml，否则加载 config/config.example.toml；LLMSettings.api_key 来自 TOML。现有 .gitignore 忽略 .env/.venv，但未忽略 config/config.toml。 | 本地真实配置可能被误提交；没有读取任何实际凭证。应忽略本地配置并验证安全注入路径。 |
| 超时、步数、取消 | Agent 有 max_steps；LLM 请求 ask_tool 有默认 timeout 300 秒并带 retry；Bash/Python/Docker 有局部超时。 | 超时不统一；未见统一任务取消、调用预算和 Windows 进程树归属。没有动态回收证据。 |
| 审批、审计、持久任务状态 | 在 app/、tests/、protocol/a2a/ 的本次搜索范围中未发现 SQLite 会话/任务事件存储、通用人工审批服务、动作账本或 result-unknown 防重放机制。AskHuman 是交互工具，不是绑定精确 diff 的审批服务。 | 对个人代码修改而言属于必须先补的安全能力；日志不能替代可恢复、可核对的审计记录。 |
| 提示注入与证据 | 未发现本地仓库索引/证据引用系统或不可信文件内容隔离协议。 | 读取项目源码后需把文件内容视为不可信输入，不能令其改变 Agent 权限或系统策略。 |

### 测试覆盖的证据边界

仓库当前 tests/ 仅有：
- tests/tools/test_browser_use_mcp.py
- tests/sandbox/test_client.py
- tests/sandbox/test_docker_terminal.py
- tests/sandbox/test_sandbox_manager.py
- tests/sandbox/test_sandbox.py

代码包含 Docker 基本命令、文件操作、超时、容器清理/manager 清理和 Fake MCP session 的测试。test_sandbox_network_access 会在测试容器中启用网络并执行安装命令；本轮没有运行任何测试。test_browser_use_mcp.py 在缺少 config/config.toml 时会创建后再删除该文件，因此本轮遵守“不运行测试”，避免触发此副作用。测试文件存在不证明其通过，也没有覆盖个人代码审批/补丁状态/跨重启审计的测试。

## M0-3：依赖和许可证

### 清单与源码导入

requirements.txt 与 setup.py 的 install_requires 有重叠但不等价的依赖集合和范围。requirements.txt 包含运行/测试/浏览器/爬虫/数据集/Docker/MCP/云服务等多类包；setup.py 漏列若干实际 import 路径需要的包。

从 main.py -> app.agent -> Manus/ToolCallAgent 的静态导入链可见以下较核心依赖：Pydantic、OpenAI SDK、tenacity、tiktoken、loguru、structlog。当前包结构的 eager imports 还会牵入 MCP、Docker SDK、boto3、requests、beautifulsoup4 和多个搜索后端；这说明在做 lazy import / extras 分层之前，不能把上游 setup.py 的安装声明当成最小 CLI 安装闭包。app/llm.py 无条件导入 app.bedrock.BedrockClient，后者 import boto3；app/agent/__init__.py 和 app/tool/__init__.py 均 eager import 多个 Agent/工具，进一步扩张 CLI 导入面。

建议的 M0 最小依赖方案（待离线安装与导入验证，不是已验证锁文件）：
- core-cli（当前源码导入闭包）：Python >= 3.12；围绕 Pydantic、OpenAI SDK、tenacity、tiktoken、loguru、structlog 确定版本；当前 eager-import 链必需的 mcp、docker、boto3、requests、beautifulsoup4 与搜索后端，在完成 lazy-import 拆分前也须暂列入该闭包或被明确改为 extras。
- 测试：pytest、pytest-asyncio。
- 按需 extras：MCP/Browser Use、Docker sandbox、Daytona（代码 import daytona，但主清单缺少显式 SDK）、A2A（协议 README 要求 a2a-sdk==0.2.5）、图像/Pillow、数据分析/pandas、浏览器/爬虫、在线搜索、AWS/Bedrock。先做真实 import 树与入口分层，再把它们从 core 移出。
- tomllib 来自 Python 3.11+ 标准库；当前 setup.py 要求 Python >=3.12，因此 tomli 在该版本目标上不是源码直接需求。numpy、datasets、huggingface-hub、uv 等未在审查到的 CLI 核心直接 import 路径中证明为必需，适合逐项按功能拆分，不能仅因在 requirements 中就列为核心。

约束不一致/风险：
- requirements.txt 对 openai~=1.66.3 比 setup.py 的 >=1.58.1,<1.67.0 更窄；pydantic、datasets、pillow 等声明的下界不同。部分约束目前有交集，但来源不统一。
- numpy、httpx>=0.27.0、tomli>=2.0.0 等没有上界；部分跨平台包的 Windows 可用性未通过安装验证。
- setup.py 未明确列出源码导入路径中的 tiktoken、boto3、mcp、docker、requests、beautifulsoup4，而 requirements 与 setup 声明另有包差异。
- 没有 lock 文件、解析结果、SBOM 或许可证清单；没有安装依赖，因此安装兼容性和传递许可证均为 NOT_VERIFIED。

许可证：
- 固定源码的 LICENSE 文件为 MIT。分发该源码时应保留原版权与许可证文本。
- 第三方运行/开发依赖需按最终锁定的具体版本审查许可证、NOTICE、模型及外部服务条款。当前未解析/安装版本，因此所有第三方许可证义务整体标为 NOT_VERIFIED；不能从项目许可证推导依赖许可。

## M0-4：个人研发 Agent 差距

| 目标 | 评估 | 现有依据 | 最小实现 |
|---|---|---|---|
| 1. 本地仓库只读问答 | ADAPT | StrReplaceEditor.view 可读文件，但没有仓库索引；默认工具集还有写文件和任意 Python。 | 先实现只读工具 profile、根目录约束与按需检索；不注册执行/写工具。 |
| 2. 文件路径、符号、行号证据 | BUILD | 未发现仓库 AST/符号索引、快照 hash、引用复核器。 | 建文件清单、函数/类符号和行范围，引用回答前重新校验内容 hash。 |
| 3. 真实 pytest 失败日志诊断 | ADAPT | 有本机/容器命令执行器，但返回码与 stderr 在 Docker FileOperator 路径不完整；无结构化 pytest 任务合同。 | 在隔离副本运行固定命令，记录退出码、stdout/stderr、测试用例和源码位置。 |
| 4. 生成补丁并展示 Diff | ADAPT | StrReplaceEditor 具备字符串替换/插入并直接写入，不提供统一的只读 patch preview。 | 先在内存/副本产生标准 unified diff，绑定基准快照和 patch hash。 |
| 5. 人工审批后隔离应用补丁 | BUILD | AskHuman 不是审批；默认本机编辑无审批，Docker 也不是审批系统。 | 建立服务端审批门，审批绑定工作区、路径、文件版本、diff hash、一次动作；仅在独立副本应用。 |
| 6. 测试失败后有限重规划 | ADAPT | max_steps、Flow 的一小时 timeout 和 PlanningTool 提供粗粒度界限；没有基于权威失败结果的有限修复状态机。 | 用确定性 Task 状态机限制模型调用/重试/重规划各自预算，并以真实退出码驱动下一步。 |
| 7. 会话、任务状态、运行日志和审计 | BUILD | Memory 是进程内列表且会丢弃最旧消息；PlanningTool 是内存字典；只有普通日志，未发现 session/run/event 持久化。 | 引入 SQLite schema 与事件序号，分离 conversation/task/run/tool_call ID，恢复只读状态且不重放未知副作用。 |
| 8. 扩展多 Agent、RAG、MCP 和评测 | ADAPT | PlanningFlow 可顺序调度 Agent；MCP client/server 已存在；没有 RAG 和代码任务固定评测集。 | 先保留 MCP 传输接口并加 allow-list；用固定单 Agent 评测和证据数据证明收益后才加入 RAG/并行 Agent。 |

## 推荐的下一阶段最小范围

M0 静态审查已完成，但整体 M0 尚未通过。先做一个只读、离线可测的 CLI 垂直切片：
1. 修正 eager imports 与可选功能边界，使只读仓库命令不需要 Bash、PythonExecute、Docker、Daytona、浏览器或外网搜索依赖。
2. 增加只读 repository snapshot/search/evidence，限制仓库根目录，输出可复核的 path:line 和快照摘要。
3. 用小型合成 Python 仓库和固定问答写离线契约测试（无模型、网络和写入）；再验证 CLI --help 和配置读取。
4. 之后才接入真实模型问答；任何编辑/测试执行留到审批与隔离执行合同实现后。

验收状态：本文件记录静态结论；所有未执行测试仍为 NOT_RUN。不要将源码测试存在或计划文本误报为功能 PASS。

## M1 只读仓库分析纵向切片（2026-10-23）

本次新增模块和确定性验收详见 [Read-only repository CLI](READ_ONLY_REPOSITORY_CLI.md)。实际实现位于 devflow_agent/repository.py、devflow_agent/cli.py、devflow_agent/tools.py；测试与合成仓库分别位于 tests/devflow_agent/test_readonly_repository.py 和 tests/fixtures/repos/readonly_demo/。

新增内容不修改固定上游 OpenManus 源码，不安装依赖。只读适配器模块本身不导入 app.agent/app.tool。M1.1 测试通过隔离导入 seam 动态加载固定提交中的真实 `app/tool/tool_collection.py` 并执行其 ToolCollection；导入期 BaseTool/ToolResult 和 logger 使用最小替身，未执行 `app.tool.__init__` 的 eager imports。正常 OpenManus 包导入和 Agent Loop 仍未运行。M1 的模型问答和证据引用校验仍未实现，故 M1 整体仍为 PARTIAL；集合执行边界状态详见 [M1.1 集成报告](M1_1_OPENMANUS_TOOL_COLLECTION.md)。
