# M0 验收记录

> 执行日期：2026-10-23（Asia/Shanghai）
> 目标基线：https://github.com/FoundationAgents/OpenManus @ 3309bf4e416fb1c74b008f3e86494439a31bad53
> 结论：**BLOCKED / 未通过**。没有进入 M1。

## 1. 前置条件与环境

| 检查 | 实际结果 |
|---|---|
| 工作区 | D:/agent/DevFlow-Agent；执行前根目录只有 docs/、plans/ 两个目录。 |
| 既有文件 | rg --files -uu 显示仅四份规划文档；无旧项目文件、Git 历史或虚拟环境。 |
| Python | python --version：Python 3.12.7，退出码 0。 |
| pip | python -m pip --version：pip 24.2，属于 Anaconda base；退出码 0。单独的 pip.exe 指向另一个 mytorch 环境，因此本轮不使用它。 |
| 测试/锁定工具 | 当前 Python 可发现 pytest；uv、pip-tools 不可用。pytest 没有在该 base 环境中执行。 |
| Docker / py launcher | docker 与 py 命令不存在。 |
| 已有虚拟环境 | 执行前没有 .venv、venv、env，VIRTUAL_ENV 与 CONDA_PREFIX 未设置。 |

## 2. 上游来源与 Git 状态

- GitHub 只读接口确认固定提交存在：SHA 3309bf4e416fb1c74b008f3e86494439a31bad53；提交日期 2026-08-16T10:29:11Z；提交说明 fix(deps): add missing structlog and cap pillow below 11。
- 该提交树包含 178 个条目；根目录不含 docs/ 或 plans/。MIT 许可证已由官方固定提交的 LICENSE 文件确认。许可证原文尚未进入本地工作树，因为源码 fetch 失败。
- 已执行 git init --initial-branch=devflow/m0，退出码 0；upstream 指向官方仓库。未运行任何 push 命令。
- git fetch --depth=1 upstream 3309bf4e416fb1c74b008f3e86494439a31bad53：退出码 1；错误为 Failed to connect to github.com port 443 after 21073 ms: Could not connect to server。
- 最终 Git 状态：No commits yet on devflow/m0；HEAD 不可解析；.git/FETCH_HEAD 存在但大小为 0。git status --short --branch 显示 .venv/、docs/、plans/ 未跟踪。GitHub 远程没有被写入。

## 3. 虚拟环境与依赖

- python -m venv .venv 首次退出码 1：ensurepip 无法写入 Windows 沙箱临时目录（PermissionError）。项目内 .venv 的 Python 解释器仍创建成功。
- 之后在 .venv 进程中将 Python 临时目录指向工作区内的临时目录，引导安装随 Python 捆绑的 pip 24.2。验证命令 .venv/Scripts/python.exe -m pip --version 退出码 0，显示 pip 位于项目 .venv。临时目录随后已从工作区移除；pip 在系统沙箱临时路径上的清理警告未尝试越权处理。
- 没有安装 OpenManus 或 DevFlow 第三方运行依赖；没有创建 requirements-devflow.in 或锁文件。缺少源码导入链和可用网络时，手写依赖版本或从 base 环境 pip freeze 都不能构成可复现锁文件。
- uv 不可用，因此没有运行 uv lock 或 uv pip compile。没有为安装 uv 而修改全局环境。

## 4. 配置、可选导入与密钥

尚未实施或验证：

- 无本地 OpenManus 配置加载器可供检查；没有添加密钥配置覆盖或配置示例。
- 没有新增 .gitignore 规则。固定提交无法检出，因此无法在保留其原 .gitignore 的基础上安全合并 .venv/、config/config.toml 与 .env 忽略规则。
- 没有审查日志中的 key 泄漏路径或修改日志代码。
- 没有改动包初始化器、模型 Provider、Agent/工具或可选依赖导入。
- 没有读取、记录或设置真实 API Key；没有调用任何模型 API，模型费用为 0。

## 5. 测试与验收命令

| 检查 | 命令/结果 | 状态 |
|---|---|---|
| Python 版本 | python --version → Python 3.12.7，退出码 0 | PASS（仅解释器检查） |
| 本地 venv | .venv/Scripts/python.exe --version → Python 3.12.7，退出码 0 | PASS（解释器存在） |
| venv pip | .venv/Scripts/python.exe -m pip --version → pip 24.2，退出码 0 | PASS（pip 可用） |
| 固定源码 fetch | git fetch --depth=1 upstream 3309bf4e416fb1c74b008f3e86494439a31bad53 → 连接 github.com:443 失败，退出码 1 | FAIL / 环境阻塞 |
| 固定 HEAD | git rev-parse --verify HEAD → Needed a single revision，退出码 1 | BLOCKED（没有本地提交） |
| 依赖锁一致性 | 未运行：尚无依赖清单或锁文件 | NOT_RUN |
| Python 语法检查 | 未运行：没有检出的源码 | NOT_RUN |
| 配置/密钥脱敏测试 | 未运行：没有检出的配置与日志模块 | NOT_RUN |
| 核心/可选导入测试 | 未运行：没有检出的源码 | NOT_RUN |
| 上游契约/回归测试 | 未运行：没有检出的源码；不运行上游沙箱网络测试 | NOT_RUN |
| CLI --help | 未运行：main.py 未检出 | NOT_RUN |
| 模型 API smoke | 按无密钥、零费用基线不运行 | NOT_RUN（预期） |

未运行的命令没有标记为 PASS。没有测试、源代码语法检查或 CLI 运行证据。

## 6. 自检与停止条件

- 没有删除或覆盖旧项目、旧仓库或现有规划文档；未执行远程写入。
- 未安装大型依赖、下载模型、修改系统代理/防火墙/安全策略或启用 Docker。
- 上游 MIT 许可证已确认；所有第三方依赖许可证仍待实际解析后审查。
- M0 验收未通过：固定 SHA 未进入本地 Git 历史，源码、配置改造、依赖锁和契约测试均未完成。由于这一基础阻塞，不启动 M1。

网络恢复后首先运行以下命令：

    git remote get-url upstream
    git fetch --depth=1 upstream 3309bf4e416fb1c74b008f3e86494439a31bad53
    git switch --detach FETCH_HEAD
    git switch -c devflow/m0-work
    git rev-parse HEAD

只有 git rev-parse HEAD 精确等于固定 SHA 后，才继续本地源码与依赖分析。届时先确认规划文件和 .venv/ 仍保留，再开始依赖解析与 M0 代码工作。

## M0 源码审查补充（2026-10-23）

本补充以当前实际工作区为准，并保留前文作为首次执行时的历史记录。首次尝试时源码未检出；当前已能在 D:/agent/DevFlow-Agent 读取项目源码和固定提交。

| 当前核对项 | 实际结果 | 状态 |
|---|---|---|
| 当前分支 | devflow/m0-openmanus | PASS |
| 当前 HEAD | 3309bf4e416fb1c74b008f3e86494439a31bad53 | PASS |
| 源码与目录 | main.py、run_flow.py、app/、config/、protocol/、tests/ 存在 | PASS（存在性） |
| .venv | 仅使用现有 Python 3.12.7 解释器运行本轮标准库专项测试；未安装依赖或修改环境 | PARTIAL |
| Git 工作区状态 | 当前 git status/git diff 仍失败；.git、index 和固定 HEAD 可读，git ls-files 查询可用；status 报 this operation must be run in a work tree，显式 --work-tree 再报 cannot come back to cwd: Permission denied。根因未确认 | PARTIAL / UNRESOLVED |
| 静态源码审查 | 已完成，详见 [OpenManus 源码审查](OPENMANUS_SOURCE_AUDIT.md) | PASS（静态范围） |
| 依赖解析、安装、锁文件及完整许可证清单 | 未进行 | NOT_RUN |
| OpenManus 语法/导入/沙箱/主 CLI、模型调用 | 未运行；本轮单独新增的 DevFlow 只读 CLI 见下方 M1 记录 | NOT_RUN |

### 当前 M0 判定

源码审查交付完成，但 M0 整体仍是 **BLOCKED / 未通过**。阻塞项包括依赖锁定与许可证清单、配置与 eager-import 边界改造、OpenManus 主 CLI/配置/Agent 动态验证，以及 Git 状态命令异常导致的工作区状态不完整确认。新建的独立只读 CLI 专项验证记录在下方 M1 小节，不代表 OpenManus 主 CLI 通过。个人代码写入能力不得启用：当前默认 Manus 有宿主机 Python 执行和无仓库根限制的直接文件编辑工具，且未见审批门与持久审计。

以上 M0 源码审查阶段未运行测试。随后 M1 只读切片新增并实际运行了专项测试；范围与结果见下方记录。仍未安装依赖、启动 OpenManus Agent、访问模型/API、修改上游业务源码、提交或推送。

## M1 只读仓库分析最小纵向切片（2026-10-23）

- 实际交付：见 [只读仓库 CLI 验收](READ_ONLY_REPOSITORY_CLI.md)。代码与测试均新增，不修改固定上游源码；无依赖安装、模型或外部 API。
- 真实执行：17 项 unittest，16 passed、1 skipped。跳过项为 OS 创建真实目录 symlink/junction：当前沙箱拒绝链接创建。越界解析分支另由确定性受控 resolver 测试通过，实际 OS link 行为仍未验证。
- CLI 帮助、索引、搜索、读取和机器可读错误均由子进程测试实际运行。测试使用临时复制的合成仓库；检索和读取前后的样例树 SHA-256 一致。
- 测试主机默认 TEMP 不可写，首次运行在 fixture setup 阶段遇到 WinError 5；之后将 TEMP/TMP 指向授权临时目录后，完整专项测试通过。没有把首次环境失败计为通过。
- 未运行 OpenManus 标准包导入、Agent Loop、上游全量测试、真实模型、Web UI 或外部 API；固定 ToolCollection 源码的隔离执行见 M1.1。未安装依赖。Git status/diff 仍异常，具体环境根因未证实。
- M0 整体仍 BLOCKED；M1 确定性检索/读取子集完成，模型问答、引用验证和真实 OpenManus 集成仍 NOT_RUN。

## M1.1 OpenManus 只读 ToolCollection 集成（2026-10-23）

- 状态：`READONLY_TOOL_COLLECTION_VERIFIED`（仅指固定版本 `ToolCollection` 源码的只读集合构造与工具调用边界；不代表 OpenManus Agent Loop 或标准包导入通过）。
- 新增 `devflow_agent/openmanus_tools.py`，要求调用方显式提供集合构造器；只构造空集合，之后注册并验证恰好两个允许工具、无重复名称、工具类型和 JSON Schema 均符合 allowlist。非空集合和非固定 OpenManus `ToolCollection` 类型 fail-closed。
- 测试从固定 HEAD 的 `app/tool/tool_collection.py` 加载真实 `ToolCollection` 类并调用其真实 `execute()`。通过最小导入 seam 避开 `app.tool.__init__`；BaseTool/ToolResult 类型和 logger 仅作导入期替身，ToolCollection 实现没有替换。该源码工作文件的 Git blob ID 与 HEAD 对应文件一致。
- 标准 `import app.tool.tool_collection` 探测因现有 `.venv` 缺少 `pydantic` 而失败；`app/tool/__init__.py` 同时静态显示会 eager import Bash、文件编辑器、搜索等不需要的模块。没有安装依赖，也没有运行默认工具工厂或 Agent Loop。
- 集成测试确认两个只读工具唯一且完整；真实搜索、读取、行范围、文件摘要、非法参数和路径穿越结果通过集合执行入口验证。测试期间监视到没有调用 `subprocess.Popen` 或创建 socket；合成仓库哈希前后不变。
- M1.1 后专项总计 19 项：18 passed、1 skipped。唯一跳过项仍为主机策略拒绝创建的 OS 目录 symlink/junction；模拟解析逃逸测试通过。
- 不代表 `Manus`、`SWEAgent` 或整个 `app.tool` 入口安全：源码中的默认 Manus 集合包含 PythonExecute、StrReplaceEditor、AskHuman、Terminate，并可在初始化/think 路径接入 MCP 工具；SWEAgent 默认包含 Bash、StrReplaceEditor、Terminate。M1.1 工厂不使用这些入口。
- 本轮未运行 Agent Loop、模型/API、MCP、Bash、PythonExecute、文件编辑、上游全量测试；没有改固定上游源码、安装依赖、提交或推送。Git `status` 异常仍未解决。

详细证据见 [M1.1 ToolCollection 集成报告](M1_1_OPENMANUS_TOOL_COLLECTION.md)。M0 整体继续 `BLOCKED`，M1 继续 `PARTIAL`。

## M1.2 OpenManus 离线 Agent Loop 集成（2026-10-23）

- 状态：`OFFLINE_AGENT_LOOP_VERIFIED`，仅限固定源码中的真实 `ToolCallAgent` 与 `ToolCollection`，以及由确定性 LLM 测试子类提供响应的离线只读流程。
- 首次真实导入发现 `app.config` 在 Daytona 配置段缺失时仍要求 `daytona_api_key`，导致标准导入报 Pydantic `ValidationError`。将该可选后端字段缺省改为空字符串；在启用 Daytona 前仍须配置真实凭证。
- `app.agent` 与 `app.tool` 包改为按需导出，保留原公开名称，避免导入只读 Agent 时 eager-import MCP、Bash、PythonExecute、文件编辑、WebSearch 等无关模块。
- 新增 `devflow_agent/openmanus_agent.py`，显式要求固定上游 Agent 类和 M1.1 的空集合工厂，并将实例限制为恰好两个只读工具、最多 12 步。Agent 的搜索、读取、工具调用配对及 memory 由真实 OpenManus Loop 执行。
- 完整回归命令 `python -m unittest discover -s tests/devflow_agent -v`：22 项运行，21 passed、1 skipped，退出码 0。唯一跳过为主机不允许创建 junction/symlink；真实 OS 链接行为仍未验证。测试使用合成仓库，前后文件 SHA-256 相同。
- 离线轨迹完成 search (`search-1`) → read (`read-2`) → 最终回答；候选搜索明确不是读取证据，读取返回 `src/totals.py` 第 3–5 行与实际 SHA-256，最终引用与该内容一致。非法工具 `python_execute` 和 `../outside.txt` 均被拒绝；三步预算测试有界停止。
- 导入与执行期间的子进程、Socket、HTTP 客户端哨兵均未触发；MCP、Bash、PythonExecute、文件编辑和 WebSearch 模块未加载，活动 Agent 工具 Schema 恰好两个只读工具。
- `.venv` 只安装本次真实导入需要的最小依赖，没有安装整份 `requirements.txt`。实际版本和用途见 [M1.2 离线 Agent Loop 报告](M1_2_OFFLINE_AGENT_LOOP.md)。没有调用真实模型/API，没有启用写入或审批。
- 此次只读授权检查成功读取 Git 工作区状态；当前分支及 HEAD 正确，原有未跟踪目录和本轮修改均保留，未提交或推送。此前默认执行环境的 Git 访问错误不再阻断状态读取。

M1.2 只证明真实 OpenManus 执行循环在确定性离线响应下调用只读工具；真实模型选择工具能力、上游全量测试、个人凭证流程、写入审批和 Task B 修复闭环仍为 `NOT_RUN`。M0 整体验收仍未通过，M1 产品级真实模型问答仍为 `PARTIAL`。

## M1.3 真实模型驱动的只读 Agent（2026-10-23）

- 状态：`PARTIAL`；真实模型门禁 `BLOCKED`。新增真实本地模型 CLI `ask`、回环端点与代理边界、单次请求/整轮预算、Provider 结束原因校验、工具调用/结果配对，以及引用必须来自可信读取内容的校验。运行时仍严格只有搜索和可信读取两个工具。
- 固定分支为 `devflow/m0-openmanus`，HEAD 仍为固定上游 `3309bf4e416fb1c74b008f3e86494439a31bad53`。没有暂存、提交或推送；原有源码、文档、计划和 `.venv` 均保留。
- 当前没有 `config/config.toml`；示例配置指向托管 Anthropic 服务且未使用；没有发现本机 llama-server、Ollama、vLLM 或 LM Studio 进程。真实模型/API 请求为 0。CLI `ask` 返回 `local_model_config_missing`、退出码 3，未创建模型客户端或发起请求。
- M1.2 修改前基线重跑：22 项，21 passed、1 skipped。修改后总专项：38 项运行，37 passed、1 skipped，退出码 0；唯一跳过为主机策略拒绝创建目录 junction。13 个 Python 文件语法检查通过，CLI 帮助退出码 0。
- 不宣称真实模型自主选工具、读取后继续推理、引用准确性或本地端点兼容性已通过。配置和本机模型服务可用后，需在合成仓库执行一次真实模型门禁并保存 CLI JSON 轨迹。详细记录见 [M1.3 真实模型只读 Agent 报告](M1_3_REAL_MODEL_READONLY_AGENT.md)。
