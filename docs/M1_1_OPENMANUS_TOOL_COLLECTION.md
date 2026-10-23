# M1.1 OpenManus 只读 ToolCollection 集成验收

日期：2026-10-23
状态：`READONLY_TOOL_COLLECTION_VERIFIED`（仅固定版 ToolCollection 的只读集合构造与调用边界）

## 验收范围

在不启动模型、Agent Loop、MCP 或副作用工具的条件下，把 M1 的两个已有只读适配器接入固定版本 OpenManus 的真实 `ToolCollection` 实现，并使用合成仓库调用其真实分发路径。

本状态不表示 OpenManus 正常包导入、BaseAgent/ToolCallAgent、Agent Loop、模型或整个上游应用已经验证。

## 基线与源码调用链

- 分支：`devflow/m0-openmanus`
- HEAD：`3309bf4e416fb1c74b008f3e86494439a31bad53`
- 固定实现：`app/tool/tool_collection.py` 中的 `ToolCollection`
- 工作文件 Git blob ID：`297ab6cbe831eebbb91fe0fdfe341794e6cddeca`
- 固定 HEAD 文件 blob ID：`297ab6cbe831eebbb91fe0fdfe341794e6cddeca`，二者一致。

静态检查确认：

1. `ToolCollection.__init__()` 保存传入工具并建立 `tool_map`；空构造 `ToolCollection()` 不会创建默认工具。
2. `to_params()` 逐项调用工具的 `to_param()`。
3. `execute(name, tool_input)` 按名称查找后调用 `await tool(**tool_input)`；集合层没有独立 JSON Schema 参数校验，只把 `ToolError` 转为 `ToolFailure`，其他未处理异常会传播。
4. `add_tool()` 对同名项发出警告并跳过；所以本适配器在注册前要求集合为空，注册后再核对工具数量、类型、名称、映射和参数 Schema。
5. `app/tool/base.py` 的 `BaseTool` 继承 Pydantic `BaseModel`，并提供 `__call__()`、`execute()`、`to_param()`。M1 适配器使用 ToolCollection 实际依赖的结构接口，不继承该 BaseTool；ToolCollection 执行路径没有运行时 `isinstance(BaseTool)` 检查。
6. `app/agent/toolcall.py` 的 `ToolCallAgent.think()` 将 `available_tools.to_params()` 交给 LLM；`execute_tool()` 检查工具名后调用 `available_tools.execute()`。本轮没有运行 ToolCallAgent 或模型。

默认工具入口与本只读 profile 分开：

- `app/agent/manus.py` 的 `Manus.available_tools` 默认包含 `PythonExecute()`、`StrReplaceEditor()`、`AskHuman()` 和 `Terminate()`；其 MCP 初始化/连接逻辑会把 MCP 工具加入集合。
- `app/agent/swe.py` 的 `SWEAgent.available_tools` 默认包含 `Bash()`、`StrReplaceEditor()` 和 `Terminate()`。
- `app/tool/python_execute.py` 的 `PythonExecute.execute()` 通过 `multiprocessing.Process` 执行 Python `exec`。
- `app/tool/bash.py` 的 `Bash.execute()` 使用 shell 会话。
- `app/tool/str_replace_editor.py` 支持 view、create、replace、insert、undo_edit 等文件操作。
- `app/tool/mcp.py` 的 `MCPClientTool.execute()` 会向 MCP session 调用远端工具；MCP clients 还支持 SSE 和 stdio transport。
- `app/tool/__init__.py` eager-import BaseTool、Bash、Crawl4ai、CreateChatCompletion、PlanningTool、StrReplaceEditor、Terminate、WebSearch 和 ToolCollection。该包初始化本身不等于这些工具已注册，但扩大导入面并引入可选依赖。

## 实现

- `devflow_agent/openmanus_tools.py` 增加 `create_openmanus_readonly_collection(workspace, tool_collection_factory)`。
- 调用方必须显式传入集合构造器。适配器直接构造空集合，不使用 Manus/SWEAgent/default-tool 工厂，也不调用后删除危险工具。
- 适配器确认集合类型模块/类名为 `app.tool.tool_collection.ToolCollection`，然后复用 `register_read_only_tools()` 注册两个现有工具。
- 注册后必须恰好有 `repository_search_readonly` 和 `repository_readonly_read` 两个唯一名称；工具对象必须分别是现有 `RepositorySearchTool`、`RepositoryReadTool`；`tool_map` 与 `to_params()` Schema 名称必须一致，否则抛出 `unsafe_registry_profile`。
- `RepositorySearchTool` 返回 `index_candidates_not_read_evidence`，候选不会冒充读取证据。`RepositoryReadTool` 使用已有授权工作区读取逻辑，返回实际文件内容、行范围、完整文件 SHA-256、片段 SHA-256 和证据标识。
- 适配器层处理 `RepoError`、参数类型/签名错误并返回结构化 JSON；意外异常不会被宣称为成功，仍会向调用端传播。
- `devflow_agent/openmanus_tools.py` 和 `devflow_agent/tools.py` 不导入 `app.*`，也不实例化任何默认工具。

## 导入边界与依赖

现有 `.venv` Python 为 3.12.7；直接探测正常 `import app.tool.tool_collection` 时，在导入 `app/tool/base.py` 处因缺少 `pydantic` 得到 `ModuleNotFoundError`。由于 `app/tool/__init__.py` 还会 eager-import其他工具，本轮没有仅安装 Pydantic 后继续追逐依赖，也没有安装 `requirements.txt`。

为验证真实集合实现，集成测试使用隔离 import seam：

- 从固定路径动态加载实际 `app/tool/tool_collection.py`，实际执行该文件中的 `ToolCollection` 类和 `execute()`。
- 使用无副作用的最小替身满足导入期 `BaseTool`/`ToolResult` 类型名称和 logger；`ToolError` 从实际 `app/exceptions.py` 加载。
- 不执行真实 `app.tool.__init__`，没有替换或复制 ToolCollection 实现；测试断言类模块和源文件路径是固定版本文件。
- 这验证了集合实现与结构化适配器的调用合同，但不证明安装完整运行依赖后的标准应用导入成功。完整包导入仍为 `NOT_RUN`。

## 确定性测试结果

命令：

```powershell
$env:TEMP = '<Codex writable temp directory>'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
& '.venv/Scripts/python.exe' -m unittest discover -s tests/devflow_agent -v
```

实际结果：**19 项运行，18 passed，1 skipped，退出码 0**。

两项新增集成测试均通过：

1. 固定 ToolCollection 中恰好注册两个工具，名称唯一，Schema 名称一致；真实搜索和读取经 `ToolCollection.execute()` 返回候选路径、真实内容、行号和 SHA-256。
2. 非法额外参数返回 `invalid_tool_arguments`；越界读取返回 `path_outside_root`；非空集合和非 OpenManus 集合类型被拒绝，非空集合不会被清理或改写。
3. 工具构造和执行期间对 `subprocess.Popen` 与 `socket.socket` 设置了失败哨兵，二者均未被调用；Bash、PythonExecute、文件编辑、MCP 和 WebSearch 模块没有载入该隔离进程。
4. 合成仓库文件哈希在测试前后相同；未创建、改写或删除授权仓库内容。
5. 先前的 CLI、索引、搜索、可信读取、路径边界、摘要、限制及错误处理测试均继续通过。

唯一跳过项是实际创建 OS 目录 symlink/junction 的测试：主机策略拒绝创建链接。独立的 resolver 模拟测试覆盖越界解析拒绝并通过；真实 OS 链接行为仍为部分未验证。`--help` 另行实际运行并正常输出帮助。

测试运行时将 `TEMP`/`TMP` 指向 Codex 提供的可写临时目录；默认临时目录此前在 fixture 创建阶段报 `WinError 5`。没有安装第三方依赖、启动模型/API、Agent Loop、MCP、Bash、PythonExecute、文件编辑或网络服务。

## 结论与限制

- `READONLY_TOOL_COLLECTION_VERIFIED`：固定版本真实 `ToolCollection` 的只读注册/执行边界通过确定性集成测试。
- `PARTIAL`：整个 M1 仍未完成模型问答、引用校验、标准 OpenManus 包导入和 Agent Loop，因此产品级代码库问答仍是部分实现。
- 本只读集合入口只暴露两个只读工具；它不改变其他 Manus/SWEAgent 默认入口。若应用其他路径仍创建默认 Agent，其危险工具仍可能存在。尚未配置或运行一个正式 OpenManus 应用入口来证明全应用只有此 profile。
- `.venv` 中缺少 Pydantic；正常 `app.tool` 导入及依赖完整性未验证。本轮没有安装依赖，因为固定 ToolCollection 源码可以通过最小隔离 import seam 直接验收，且宽泛包初始化会引入无关工具依赖。
- Git `status` 仍报 `fatal: this operation must be run in a work tree`；HEAD 和固定源码 blob 可核对，`git ls-files --modified` 与暂存区查询为空，但无法凭 `git status` 证明整个工作区干净。没有暂存、提交或推送。

## 下一步

在不运行模型的前提下，单独建立完整依赖环境和安全导入边界，再以一个只接收此 allowlist 集合的自定义 Agent 入口验证真实 OpenManus Agent 调用链。只有该入口可证明不加载默认工具后，才考虑模型问答；不得把本次集合测试扩展解释为完整 Agent 验收。
