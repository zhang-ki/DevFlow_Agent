# M1.2 OpenManus 离线 Agent Loop 集成验收

执行日期：2026-10-23（Asia/Shanghai）
状态：`OFFLINE_AGENT_LOOP_VERIFIED`（仅指固定源码的确定性离线 Agent Loop）

## 验收范围

使用固定 OpenManus 源码中的真实 `ToolCallAgent`、`ToolCollection`、`Message`、`ToolCall` 和 `LLM` 类，在合成仓库上运行一次有界检索与可信读取流程。模型响应由真实 `LLM` 类型的确定性测试子类提供；本轮没有调用模型、外部 API、MCP 服务或写入工具。

固定基线：分支 `devflow/m0-openmanus`，HEAD `3309bf4e416fb1c74b008f3e86494439a31bad53`。原有 `docs/`、`plans/`、`.venv/` 和 M1.1 证据均保留。没有提交或推送。

## 根因与最小改动

真实导入首先被配置默认值阻断：`app/config.py` 在没有 Daytona 配置段时仍构造 `DaytonaSettings()`，但 `daytona_api_key` 是必填字段。固定 `config.example.toml` 没有 Daytona 配置时，标准导入因此抛出 Pydantic `ValidationError`。

另外，`app/agent/__init__.py` 和 `app/tool/__init__.py` 原先 eager-import 所有 Agent 和工具。导入只读 `ToolCallAgent` 也会加载 Browser/MCP/SWE 与 Bash、文件编辑、WebSearch 等无关模块，并要求本轮不需要的可选依赖。

本轮只改了三处上游文件：

- `app/config.py`：将 Daytona API key 的缺省值设为空字符串；实际启用 Daytona 时仍必须由运行环境提供有效凭证。
- `app/agent/__init__.py`：保留原有公开导出名，改为按需导入。
- `app/tool/__init__.py`：保留原有公开导出名，改为按需导入。

另新增：

- `devflow_agent/openmanus_agent.py`：显式接收固定 OpenManus Agent 类和 M1.1 工具集合构造器，创建只包含两个只读工具的有界 Agent。该窄子类只补充“无工具调用的最终助手回复即结束”行为；搜索、读取和工具分发仍由真实 OpenManus Agent/ToolCollection 实现执行。
- `tests/devflow_agent/test_openmanus_agent_loop.py`：正常包导入、真实 Agent Loop 和副作用哨兵测试。

不变量：活跃 Agent 工具集合必须精确为 `repository_search_readonly` 与 `repository_readonly_read`。每次实例构造重新核验集合；步数限制为 1 到 12，默认 8。适配器拒绝非真实 `app.llm.LLM` 实例，防止 OpenManus 隐式创建 Provider。未注册工具由真实 `ToolCallAgent.execute_tool()` 拒绝。读取工具继续使用 M1 的授权根目录、路径规范化、实际磁盘 SHA-256 与行号证据。

## 实际导入和调用链

在项目 `.venv` 中使用正常 Python 包导入路径成功导入：

1. `app.agent.toolcall.ToolCallAgent`，继承真实 `app.agent.react.ReActAgent` 和 `app.agent.base.BaseAgent`。
2. `app.tool.tool_collection.ToolCollection`、`app.tool.base.BaseTool`、`app.tool.create_chat_completion.CreateChatCompletion` 和 `app.tool.terminate.Terminate`。
3. `app.schema.Message`、`ToolCall`、`Function` 与 `app.llm.LLM`。
4. 实际依赖的配置、日志及 Sandbox 客户端模块。

集成测试调用真实 `ToolCallAgent.run()` → `think()` → `available_tools.to_params()` → 原生 `ToolCall` 参数解析 → `execute_tool()` → `ToolCollection.execute()` → 两个 M1 只读适配器；工具结果以匹配的 `tool_call_id` 写回真实 Agent memory，再由下一次模型响应产生最终回答。

实际轨迹：

| 回合 | 模型替身给出的响应 | 真实执行结果 |
|---|---|---|
| 1 | 调用 `repository_search_readonly(query="sum numeric values", limit=5)`，ID `search-1` | 返回真实候选 `src/totals.py`，状态明确为 `index_candidates_not_read_evidence`。 |
| 2 | 读取 `src/totals.py` 第 3–5 行，ID `read-2` | 返回真实源码、`trusted_file_read`、行范围、完整文件 SHA-256 和片段摘要。下一次模型输入含有匹配 ID 与完整工具结果。 |
| 3 | 返回最终说明和引用 | 回答引用 `src/totals.py:3-5`，内容与读取到的 `return sum(values)` 一致。 |

坏路径也经过 Agent Loop：`python_execute` 被拒绝为未注册工具；`../outside.txt` 经只读工具返回 `path_outside_root`。循环边界测试使用 3 步上限并确认恰好 3 次调用后终止。非法 Agent 类及超过 12 步的配置被拒绝。

## 导入和副作用边界

- 实际使用正常的 `app.agent`、`app.tool` 公共包初始化器及其懒加载导出；没有用替身替换 Agent、ToolCollection、Message、LLM 或工具实现。
- 导入和运行阶段对 `subprocess.Popen`、`subprocess.run`、asyncio 子进程创建、`socket.socket`、`httpx.Client`、`httpx.AsyncClient` 设置失败哨兵；测试通过。
- `app.agent.mcp`、`app.tool.mcp`、Bash、PythonExecute、文件编辑和 WebSearch 模块未进入已导入模块集合；Agent 活跃 Schema 和 `tool_map` 均只包含两个只读工具。
- 测试在发现本地 `config/config.toml` 或 `config/mcp.json` 时会拒绝运行；此次使用固定示例配置，不读取个人 API 配置。
- Loguru 文件 sink 在导入测试中关闭，避免产生项目日志文件。没有创建 MCP session、网络服务、子进程或模型客户端请求。
- 测试前后合成仓库文件树的 SHA-256 完全一致。

懒加载只保证按需访问公开类时不导入无关工具；显式请求 `Bash`、MCP、SWEAgent、BrowserAgent 或使用 `from ... import *` 仍会按需导入相应功能。本轮没有验证这些可选 Agent 的依赖或执行行为。

## 依赖

原 `.venv` 只有 pip。为加载真实 OpenManus 类，在专属 `.venv` 中安装了直接依赖及导入链依赖；未安装整份 `requirements.txt`：

| 包 | 安装版本 | 用途 |
|---|---:|---|
| pydantic | 2.10.6 | Agent、消息、工具及配置模型 |
| openai | 1.66.5 | 实际 `app.llm` 模块导入；测试替身不创建请求 |
| tenacity | 9.0.0 | `app.llm` 的重试装饰器 |
| tiktoken | 0.9.0 | `app.llm` 的 token 计数器 |
| loguru | 0.7.3 | OpenManus logger 模块 |
| structlog | 25.5.0 | 实际 `BaseTool` 导入的日志依赖 |
| boto3 | 1.37.38 | `app.llm` 导入的 Bedrock Provider 模块；未创建客户端或调用服务 |
| docker | 7.1.0 | BaseAgent 导入的 Sandbox 类型依赖；未连接 Docker daemon |

安装只发生在项目 `.venv`。MCP、Browser Use、搜索引擎、Docker 服务端、模型权重和 API 凭证均未安装或使用。没有生成锁文件，也没有完成完整依赖许可证扫描。

## 测试结果

命令：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:TEMP = '<repo-root>\.venv'
$env:TMP = $env:TEMP
& '.venv\Scripts\python.exe' -m unittest discover -s tests\devflow_agent -v
```

结果：**22 项运行，21 passed、1 skipped，退出码 0**。

- 新增真实 Agent Loop 测试：搜索→读取→最终回答、调用 ID/消息顺序、引用/摘要一致性和副作用哨兵通过。
- 非法工具名与越界路径拒绝通过。
- 有界步数、错误推进及不可信 Agent 类拒绝通过。
- M1.1 ToolCollection 构造/执行回归及 M1 CLI、索引、路径、摘要和限制测试通过。
- 唯一跳过：主机策略不允许创建真实目录 junction/symlink；解析逃逸的确定性测试通过，但真实 OS 链接行为仍未验证。
- 有上游 Pydantic v2 迁移警告（旧 `Config`/`dict()` 用法），未影响本轮结果。适配器拒绝隐式 Provider 的回归也通过。

未运行真实模型/API、MCP、Agent 网络功能、默认 Bash/PythonExecute/文件编辑工具、上游全量测试、Browser/UI 或代码写入。确定性离线结果不代表真实模型选工具能力。

## Git 和交付状态

通过只读授权读取 Git 状态后确认固定分支/HEAD 正确。原有目录保持未跟踪状态；本轮三个上游文件显示为修改，M1/M1.2 源码、测试、报告及计划仍显示为未跟踪。本轮未暂存、提交或推送。

早先默认执行环境中的 `git status` 因访问边界报错；本轮经受限授权读取后，Git 能正常报告状态。没有证据显示 Git 元数据损坏。

## 结论与限制

`OFFLINE_AGENT_LOOP_VERIFIED` 仅覆盖：固定 OpenManus `ToolCallAgent` 和 `ToolCollection` 的真实标准导入路径、确定性离线响应、两个只读工具、消息/调用 ID 关联、引用证据和有界停止。

仍未验证：真实模型工具调用、个人配置/凭证流程、上游全部工具和 Agent、MCP/浏览器/外部服务、持久任务历史、写入审批及真实修复闭环。PERSONAL/真实仓库写入能力没有实现或启用。

下一步最小范围：在独立合成仓库上接入一个真实本地模型端点，先只验证搜索与读取工具调用和引用核对；仍不加入写入工具。
