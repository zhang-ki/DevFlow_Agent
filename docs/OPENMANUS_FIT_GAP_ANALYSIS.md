# OpenManus 适配性与差距分析

> 调研日期：2026-10-23（Asia/Shanghai）
> 源码基线：FoundationAgents/OpenManus，默认分支 main，提交 3309bf4e416fb1c74b008f3e86494439a31bad53（提交日期 2026-08-16）。
> 本文依据固定提交的实际源码、配置、测试树和许可证做静态调研；调研阶段没有克隆、安装依赖、运行项目或测试。除明确标为 VERIFIED 的内容外，不推断运行结果。

## 1. 仓库身份与源码边界

目标 URL 确认指向 FoundationAgents 组织下的公开仓库 [FoundationAgents/OpenManus](https://github.com/FoundationAgents/OpenManus)。仓库默认分支是 main；GitHub API 返回当前 main 指向提交 3309bf4e416fb1c74b008f3e86494439a31bad53，提交信息为依赖修正（补充 structlog、限制 Pillow 版本）。提交签名状态为 unsigned。后续实施应固定该 SHA，而不是依赖会变化的 main。仓库 [LICENSE](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/LICENSE) 是 MIT。

本工作区 D:/agent/DevFlow-Agent 在调研阶段起始为空且不是 Git 仓库；当时只创建规划文档，没有改动旧 Agentexpr 项目或任何旧仓库，也没有访问或写入远程仓库。M0 的后续执行状态记在 [M0 验收记录](M0_ACCEPTANCE.md)。

### 项目目录与启动入口

固定提交根目录包含 app、config、examples、protocol/a2a、tests、workspace、Dockerfile、main.py、run_flow.py、run_mcp_server.py、sandbox_main.py、requirements.txt 和 setup.py。根目录没有 pyproject.toml 或已提交的 uv.lock。

| 用途 | 代码证据 | 结论 |
|---|---|---|
| 单 Agent CLI | [main.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/main.py) 创建 Manus 并调用 run | 交互式通用 Agent 入口 |
| 计划流程 CLI | [run_flow.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/run_flow.py) 创建 Manus/DataAnalysis，再用 FlowFactory 创建 PlanningFlow | 有计划式流程入口；不是 Web API |
| Agent 框架 | [app/agent/base.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/base.py)、[react.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/react.py)、[toolcall.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/toolcall.py) | BaseAgent 状态循环、ReAct think/act 抽象、工具调用 Agent |
| 工具与扩展 | [app/tool](https://github.com/FoundationAgents/OpenManus/tree/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool)、[app/tool/mcp.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/mcp.py) | 本地工具、MCP 客户端及工具注册 |
| 模型与配置 | [app/llm.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/llm.py)、[app/config.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/config.py)、[config/config.example.toml](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/config/config.example.toml) | TOML 配置；支持若干模型客户端分支 |
| 流程与计划 | [app/flow](https://github.com/FoundationAgents/OpenManus/tree/3309bf4e416fb1c74b008f3e86494439a31bad53/app/flow)、[app/tool/planning.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/planning.py) | 计划是工具和 Flow 层代码；本提交没有 app/agent/planning.py |
| 容器与部署 | [Dockerfile](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/Dockerfile)、[sandbox_main.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/sandbox_main.py) | Dockerfile 安装完整 requirements 后以 bash 作为 CMD，属于交互容器，不是已验证的服务部署镜像；sandbox_main.py 使用 Daytona 云沙箱并提供预览 URL |
| 测试 | [tests/sandbox](https://github.com/FoundationAgents/OpenManus/tree/3309bf4e416fb1c74b008f3e86494439a31bad53/tests/sandbox)、[tests/tools](https://github.com/FoundationAgents/OpenManus/tree/3309bf4e416fb1c74b008f3e86494439a31bad53/tests/tools) | 可见测试集中在工具与 Docker 沙箱 |

README 的运行说明不是运行证据。本轮没有验证该提交能否在当前 Windows 主机启动，也没有确认其测试通过状态：两项均为 UNKNOWN。

## 2. 实际架构与证据

### Agent Loop、模型与工具调用

- BaseAgent 使用 Pydantic 模型保存状态和 Memory；run() 在 IDLE 时进入 RUNNING 循环，受 max_steps 限制，并通过重复 assistant 内容检测卡住状态。循环结果是字符串。见 [base.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/base.py#L105-L168)。
- ReActAgent 抽象 think()、act()，step() 顺序执行二者。ToolCallAgent 的 think() 调用 LLM.ask_tool()，传递 JSON Schema 风格的 tools 与 tool_choice；act() 按模型返回的列表逐个执行工具。JSON 解析、未知工具和工具异常会转成观察文本交给模型；这提供恢复机会，但不保证模型能正确恢复。见 [react.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/react.py)、[toolcall.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/toolcall.py#L35-L196)。
- app/llm.py 的 LLM 是客户端门面，不是完整插件化 Provider 注册系统。api_type 分支创建 OpenAI-compatible AsyncOpenAI、Azure AsyncAzureOpenAI 或 BedrockClient；还包含 tiktoken 计数、可选 max_input_tokens 和工具调用请求。ask()/ask_tool() 有 Tenacity 重试（最多六次，异常类型较宽）。见 [llm.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/llm.py#L174-L229)。
- 工具由 BaseTool 暴露名称、描述、参数 schema 和 async execute；ToolCollection 按名称分发。SWEAgent 注册 Bash、StrReplaceEditor、Terminate。Manus 默认注册 PythonExecute、StrReplaceEditor、AskHuman、Terminate，并尝试通过 MCP 初始化 Browser Use。见 [swe.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/swe.py)、[manus.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/manus.py)。

### 多 Agent、计划与恢复

BaseFlow 保存多个 Agent；PlanningFlow 从计划步骤的 type 选择 executor。run_flow.py 当前只装配 Manus，配置允许时再增加 DataAnalysis。PlanningFlow 的 execute() 逐步取步骤并 await 一个 executor.run()，没有并行 fan-out、Agent 间共享的结构化证据协议或冲突合并器。见 [base.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/flow/base.py)、[planning.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/flow/planning.py#L45-L135)。

PlanningFlow 的计划来自 LLM 调用 PlanningTool；流程没有测试结果、退出码或语义成功校验的概念。_execute_step() 在 executor.run() 正常返回后就标记步骤完成。若 Agent 把工具失败作为文本返回而不是抛异常，Flow 仍可能把该步骤标成 completed。这不能充当 CI 修复状态机或安全审批层。见 [planning.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/flow/planning.py#L277-L339)。

protocol/a2a 是实验性集成；其 README 标记为 non-streaming，Agent 的 stream() 明确抛出 NotImplementedError。它可作为互操作研究，不证明内部多 Agent 并行编排已实现。见 [A2A README](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/protocol/a2a/app/README.md)、[A2A agent.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/protocol/a2a/app/agent.py)。

### 记忆、上下文与持久化

app/schema.py 的 Memory 只保存进程内 Message 列表，默认最多 100 条，超出后截掉更早消息；未找到数据库、会话恢复或长期记忆实现。LLM 有 token 计数及可选累计输入预算，但 Memory 裁剪不是 token-aware 历史摘要。BaseAgent 在达到 max_steps 时才重置步数；复用 Agent 实例时内存和步数生命周期需要单独测试或重置。见 [schema.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/schema.py#L54-L181)、[base.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/base.py#L119-L139)。

日志使用 Loguru/Structlog；Agent 步骤和工具结果有日志，CLI 返回汇总字符串。没有发现规范化的 RunEvent、任务事件持久化、WebSocket/SSE 任务事件通道或从失败事件恢复 Agent 状态的模块。

### 工具扩展、结构化输出与测试

- MCP 客户端支持 stdio 和 SSE 连接，发现远程工具并转成 ToolCollection；MCPAgent 会刷新工具 schema。OpenManus 也有 MCP server 启动入口。见 [MCP client](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/mcp.py)、[MCPAgent](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/mcp.py)、[MCP server](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/mcp/server.py)。
- SandboxManus 使用 Daytona API 创建远程沙箱，需 daytona 配置/API key；它不是本地、无外部服务成本的 M3 默认方案。见 [sandbox_agent.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/agent/sandbox_agent.py)。
- CreateChatCompletion 可从 Pydantic BaseModel 生成参数 JSON Schema，并在执行时构造模型对象；这只是部分结构化输出能力，不是所有 Issue/诊断/补丁报告的端到端 Schema 校验。见 [create_chat_completion.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/create_chat_completion.py)。
- 可见测试树包含 sandbox 与 tools。MCP 工具测试使用 FakeSession；沙箱测试依赖 Docker。test_sandbox.py 有一项显式 network_enabled=True 的测试，会运行 apt update、apt install curl 并访问公网。当前不应把该测试视为无需外部影响的离线单测。见 [test_browser_use_mcp.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/tests/tools/test_browser_use_mcp.py)、[test_sandbox.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/tests/sandbox/test_sandbox.py#L64-L85)。
- 未在 tests 树发现 Agent Loop、PlanningFlow、SWE 修复闭环、repo 检索或固定代码修复评测集。该判断来自当前可见目录树，不等于对其他分支或未纳入仓库资源作保证。

## 3. 八项能力差距

标签含义：EXISTING_VERIFIED = 源码与测试证据支持其基础功能；EXISTS_BUT_INCOMPLETE = 有可复用基础，但达不到目标；NEEDS_EXTENSION = 需要扩展现有模块；NEEDS_IMPLEMENTATION = 关键能力尚无证据；BLOCKED = 依赖外部条件，本次未用于掩盖实现缺口。

| 能力 | 状态 | 当前证据与差距 |
|---|---|---|
| 1. 多 Agent 协同与并行编排 | EXISTS_BUT_INCOMPLETE | PlanningFlow 能按步骤选择多个 Agent，但顺序执行。没有并行调查、结果冲突处理、合并协议或可靠的完成判据。 |
| 2. 仓库结构化索引、混合检索、证据引用 | NEEDS_IMPLEMENTATION | SWEAgent 可操作文件，但没有仓库索引、代码符号/文本检索层、引用对象或可校验的 path:line 证据。 |
| 3. Agent Loop、原生工具调用、错误恢复、有限重规划 | EXISTS_BUT_INCOMPLETE | 有 ReAct、OpenAI-style tool calling、工具异常观察文本、max_steps 和卡住检测；没有测试结果驱动的重规划、总调用/费用预算和可靠 Agent 生命周期边界。 |
| 4. Issue 分诊、PR Review、CI Debug 专用 Agent | NEEDS_IMPLEMENTATION | SWEAgent 是通用编码 Agent。未发现 GitHub Issue/PR 或真实 CI 日志专用工作流。MCP 自定义工具不能替代专用逻辑和证据协议。 |
| 5. 权限、补丁预览、人工审批、审计 | NEEDS_IMPLEMENTATION | 有 AskHuman 和编辑工具的 undo_edit，但无策略引擎、补丁预览 hash、审批闸门、目录白名单或结构化审计记录。 |
| 6. 会话持久化、上下文预算、摘要、长期记忆 | EXISTS_BUT_INCOMPLETE | 有进程内消息历史、条数上限和 token 计数/可选预算；无持久化、token-aware 摘要、跨进程恢复或长期记忆。 |
| 7. 实时任务状态、执行轨迹、故障追踪、历史恢复 | EXISTS_BUT_INCOMPLETE | 有文本日志、步骤日志和工具输出；无持久化事件模型、任务进度协议或运行中断后的恢复点。 |
| 8. 固定评测集、回归测试、成功率、消融 | NEEDS_IMPLEMENTATION | 有工具/沙箱单测，但没找到代码任务金标评测集、基线指标、消融或修复成功率报告。 |

### 额外评估

| 方向 | 状态 | 结论 |
|---|---|---|
| MCP | EXISTING_VERIFIED | stdio/SSE 客户端、工具注册和 FakeSession 测试均有代码证据。应在 DevFlow 中收紧到 allow-list；不要自动信任任意 MCP server instructions。 |
| Skill Runtime | NEEDS_IMPLEMENTATION | 有 Agent prompt、工具 schema 和 MCP server 指令；没有可复用 Skill 生命周期、版本、输入输出 schema、权限声明和评测注册表。 |
| 结构化输出与 Schema 校验 | EXISTS_BUT_INCOMPLETE | CreateChatCompletion 支持 Pydantic schema 生成和实例构造；核心任务产物尚无统一严格校验。 |
| 前后端交互与实时进度 | NEEDS_IMPLEMENTATION | 有 CLI 和 MCP server；没有面向任务的 Web UI、Run Event API 或 SSE 进度通道。 |
| 隔离工作区、测试执行和进程清理 | EXISTS_BUT_INCOMPLETE | 有 DockerSandbox、CPU/内存/超时、cleanup 和沙箱测试；但默认 use_sandbox=False，Docker exec 为 root 且 privileged=True。 |
| 测试失败到修复后通过的闭环 | NEEDS_IMPLEMENTATION | 没有识别测试失败、关联源码、预览补丁、审批、应用、重测和有限重规划的流程。 |

## 4. 权限与高风险边界

这里必须按源码实际实现，而不是按工具描述里的 “sandbox” 字样推断：

1. [Bash](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/bash.py) 使用 asyncio.create_subprocess_shell 启动 /bin/bash；未见该工具本身把命令送进 DockerSandbox。native Windows 环境也不能假定 /bin/bash 可用。
2. [PythonExecute](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/python_execute.py) 在子进程执行 exec，提供超时，但 safe_globals 含完整 Python builtins；超时不等价于权限隔离。
3. [StrReplaceEditor](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/tool/str_replace_editor.py) 默认由 LocalFileOperator 操作文件；validate_path 检查绝对路径、存在性和命令组合，没有把路径约束到目标仓库根目录。SandboxSettings.use_sandbox 默认为 false。配置文件是普通 TOML，读取逻辑没有环境变量替换；当前 .gitignore 未忽略 config/config.toml。
4. DockerSandbox 有内存、CPU、网络和超时控制，但 DockerSession.exec_create 设置 privileged=True、user=root。默认配置网络关闭；这不抵消容器进程权限过大的问题。见 [config.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/config.py#L94-L108)、[sandbox.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/sandbox/core/sandbox.py#L49-L102)、[terminal.py](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/app/sandbox/core/terminal.py#L40-L65)。

结论：复用 Agent 与模型接口，不等于复用其写文件和执行 shell 的权限策略。M1 默认只读。M3 必须实现新的仓库根目录约束、补丁审批和隔离执行边界。

## 5. 许可证与依赖风险

| 项目 | 基线证据 | 风险/处理 |
|---|---|---|
| OpenManus | [MIT LICENSE](https://github.com/FoundationAgents/OpenManus/blob/3309bf4e416fb1c74b008f3e86494439a31bad53/LICENSE) | 可复用；若分发源码或实质性部分，保留版权与许可证文本。不要把 MIT 理解成免除第三方依赖义务。 |
| pydantic / pydantic-core | requirements.txt / setup.py 有依赖；上游 [Pydantic](https://github.com/pydantic/pydantic/blob/main/LICENSE) 与 [pydantic-core](https://github.com/pydantic/pydantic-core/blob/main/LICENSE) 为 MIT | 核心依赖，仍须把实际解析版本列入 M0 锁文件与许可证清单。 |
| openai Python SDK | setup.py 限定 1.x；上游 [LICENSE](https://github.com/openai/openai-python/blob/main/LICENSE) 为 Apache-2.0 | 与 OpenManus MIT 义务分别管理；保留相应许可证和 NOTICE。 |
| MCP Python SDK | OpenManus 要求 mcp~=1.5.0；[v1.5.0 LICENSE](https://github.com/modelcontextprotocol/python-sdk/blob/v1.5.0/LICENSE) 为 MIT | 应锁定解析出的 1.5.x 版本。不要拿 SDK 当前主分支的许可证信息替代被解析版本。 |
| html2text | requirements.txt 与 setup.py 均包含 html2text~=2024.2.26；上游 README 标注 [GPLv3](https://github.com/Alir3z4/html2text) | 对分发 DevFlow 的影响需单独法律评估。该依赖不属于仓库问答/CI 修复核心，建议在最小依赖集排除，除非确认需要并完成许可证审查。 |
| Crawl4AI | requirements.txt 含 crawl4ai~=0.6.3；[v0.6.3 LICENSE](https://github.com/unclecode/crawl4ai/blob/v0.6.3/LICENSE) 为 Apache-2.0，另有具体 attribution 条款 | 如分发或公开使用，需要按该版本许可证履行 attribution。MVP 不需要网页爬虫，应放入可选 extras 或移除运行路径依赖。 |
| 其余直接与传递依赖 | requirements.txt、setup.py | 本次未逐一审完全部许可证，也没有锁定解析版本/SBOM；状态为 UNKNOWN。必须在 M0 固定锁文件后生成完整清单，不能声称传递依赖全部兼容。 |

requirements.txt 与 setup.py 的依赖集合和版本约束不一致，requirements 中含大量浏览器、爬虫、数据集依赖，而源码测试并未证明这些都是 DevFlow 核心运行所需。根目录没有 lock。M0 应只解析 Agent/LLM 核心导入链、pytest，以及按需加入的 Docker 依赖；生成精确锁文件和第三方许可证清单。不要整份安装 requirements.txt 来获得一个小的只读原型。

## 6. 可复用、要改造、要新增

| 处理方式 | 模块 |
|---|---|
| 复用 | app/agent/base.py、react.py、toolcall.py 的状态/循环与工具调用基础；app/llm.py 的模型客户端和 token 计数；app/schema.py 中可用的消息结构；ToolCollection/BaseTool 协议；MCP 客户端（默认关闭、显式 allow-list）。 |
| 改造 | Agent 的任务生命周期、步数/消息重置；模型错误重试与费用预算；PlanningFlow 的完成判据；工具注册权限；配置密钥处理；测试与可选依赖边界。 |
| 新增 | 仓库索引与证据引用；Issue/CI 任务合同；诊断/补丁 Pydantic schema；策略与审批门；隔离的测试运行器；审计事件存储；任务恢复；固定评测集和对比报告。 |

## 7. 结论

最值得复用的是 ReAct/工具调用与 LLM 客户端封装、可扩展的 ToolCollection/MCP 接口，以及现有 Docker 生命周期的部分实现。最需要重做的是通用 PlanningFlow 作为 DevFlow 控制平面的角色、仓库证据检索、状态/持久化，以及整个文件写入与命令执行权限边界。建议 M0 先固定上游提交并处理可复现安装；M1-M3 做出单 Agent、带证据、受审批和沙箱约束的纵向闭环；只有获得单 Agent 指标后，再评估并行多 Agent 的价值。

## 8. M0 补充核验

2026-10-23 通过 GitHub 官方只读接口再次确认，`main` 当前仍指向固定提交 `3309bf4e416fb1c74b008f3e86494439a31bad53`；提交树含 178 个条目，根目录没有 `docs/`、`plans/`。固定提交的 MIT LICENSE 已核实。但本机 Git 对 github.com:443 的 fetch 失败，源码没有检出到本工作区，因此本分析仍是远程源码静态研究，不代表 M0 已建立可运行本地基线。验收命令和退出状态见 [M0 验收记录](M0_ACCEPTANCE.md)。
