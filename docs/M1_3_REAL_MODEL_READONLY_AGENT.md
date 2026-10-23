# M1.3 真实模型驱动的只读 Agent 验收

日期：2026-10-23
阶段状态：`PARTIAL`
真实模型门禁：`FAIL`（模型服务已运行，但单次 Agent 验收因越界读取参数安全停止）

## 摘要

M1.3 仍为 `PARTIAL`。2026-10-23 使用本机 Qwen3.5-4B 完成了一次真实、受限的 `ask`：模型自主先调用仓库搜索，再选择读取 `src/totals.py`。本地 llama.cpp 服务健康检查和模型加载均成功，确定性回归最终为 38 项运行、37 passed、1 skipped。

真实门禁未通过：模型请求读取第 1–20 行，但文件实际只有 10 行；可信读取工具返回 `invalid_line_range`，Agent 按现有 fail-stop 合同结束，未取得文件读取证据、未继续发起第三次模型请求，也没有最终答案或有效引用。因此不得标记 `REAL_MODEL_READONLY_AGENT_VERIFIED`。完整单次运行证据见 [本次 JSON 报告](../reports/public/M1_3_REAL_MODEL_ACCEPTANCE_2026-10-23.json) 和同目录原始日志。

本机有 RTX 4060，但盘点到的所有现有 llama.cpp 二进制均报告 `Available devices: (none)`；按用户允许使用 GPU 的指示检查后，确认当前没有已安装 GPU 后端，因此本次使用已有 CPU 运行时，没有安装或下载替代组件。
## 基线和保护

- 分支：`devflow/m0-openmanus`
- HEAD：`3309bf4e416fb1c74b008f3e86494439a31bad53`，与固定上游 SHA 一致。
- 固定上游提交是当前 HEAD；没有检出或重置。
- 本轮开始时无暂存内容，3 个已有跟踪文件修改，21 个已有未跟踪文件/目录；均予以保留。
- 本轮没有暂存、提交或推送。`.venv` 保持在项目内；未安装依赖、下载模型或启动服务。
- 开始前 M1.2 基线：22 项运行，21 通过、1 跳过，退出码 0。

## 实现

| 文件 | 变更 |
|---|---|
| `devflow_agent/real_readonly_agent.py` | 新增 loopback-only 模型配置读取、直接 TCP 可达性预检、禁用环境代理的 AsyncOpenAI 客户端、单次 Provider 调用绑定、有界运行、工具调用/结果 ID 配对、可信读取提取和最终引用校验；客户端初始化或单次调用适配器设置失败时关闭已创建的 HTTP/SDK 客户端。 |
| `devflow_agent/cli.py` | 新增 `ask --repo --question [--max-steps] [--timeout-seconds]`。成功时输出包含真实调用参数、工具结果、读取证据、最终答案和引用校验的有界 JSON；被阻断时输出结构化错误。 |
| `devflow_agent/openmanus_agent.py` | 保留真实 OpenManus Loop 和现有只读 ToolCollection；每个模型决策最多一个工具调用；模型 Provider 异常、截断/结束原因异常、工具错误和无有效回答均记录终止原因并停止。 |
| `app/llm.py` | `LLM.ask_tool()` 保存 Provider `finish_reason`，使上层能在执行工具前拒绝 `length` 或其他不完整响应。 |
| `app/logger.py` | 支持本进程环境开关关闭 OpenManus 文件日志并降低日志级别；实时只读入口启用该模式，避免把提示词和源码摘录写入默认日志文件。 |
| `tests/devflow_agent/test_real_readonly_agent.py` | 新增本地模型配置、loopback 限制、密钥不回显、工具调用配对、引用校验、保守 tokenizer、单次请求绑定、初始化失败资源清理、整轮超时取消和无文件日志测试。 |
| `tests/devflow_agent/test_openmanus_agent_loop.py` | 更新错误工具/越界读取为立即终止，并增加真实 `LLM` finish_reason 传播、截断拒绝和缺少完整工具结束标记时零工具执行测试。 |
| `tests/devflow_agent/test_readonly_repository.py` | CLI 帮助测试确认包含 `ask`。 |
| `docs/READ_ONLY_REPOSITORY_CLI.md`、`docs/M0_ACCEPTANCE.md`、`plans/ROADMAP.md` | 更新能力状态、运行命令、验收事实和下一步条件。 |

### 运行合同

- 只接受明确存在的 `config/config.toml`；不回退到 `config.example.toml`。
- 只允许 `localhost` 或 IP loopback 的 HTTP(S) OpenAI-compatible endpoint；URL 内嵌凭证、查询参数、非 loopback 域名和非支持 API 类型均拒绝。
- 先进行一次直接 loopback TCP 探测；模型 HTTP 客户端使用 `trust_env=False`，不会经过继承的 HTTP 代理。
- 活跃 ToolCollection 由 M1.1 工厂再次核验，恰好是 `repository_search_readonly` 与 `repository_readonly_read`。没有 Bash、PythonExecute、编辑器、MCP 或写入工具。
- 每个模型决策只允许一个工具调用。OpenManus 原始 `ask_tool` 逻辑复用，但本入口调用其未装饰实现，禁用上游 6 次 Tenacity 重试；OpenAI SDK 重试设为 0。
- 默认最多 6 个模型决策、180 秒整轮截止、每次请求最多 120 秒、输出最多 1024 tokens；CLI 的 hard cap 分别为 12 步和 300 秒。
- 本地入口使用保守 UTF-8 字节估算，避免未知模型名触发 tiktoken 在线下载。该值仅为估算，不等价于模型 tokenizer；配置了 `max_input_tokens` 时只能作为近似拦截。
- `finish_reason=length`、缺少完整工具结束标记、Provider 异常和工具错误均不会被当成完成；失败终态带原因码。
- 回答需包含支持文件类型的 `relative/path.py:起止行` 等引用；每个引用必须位于成功的可信读取结果范围内。搜索候选不能证明读取过文件。
- 实时入口关闭 OpenManus 文件日志。CLI 将有界工具轨迹 JSON 输出到标准输出；本次真实运行的两次模型请求与两次工具调用已保存为 JSON 和原始服务日志；此前初始实现阶段未运行模型的记录属于历史状态，现由本次真实运行结果更新。

## 可用模型与真实门禁结果（2026-10-23）

本轮只读检查了 `<model-root>`，没有复制权重进仓库。清单如下：

| 模型 | 权重路径与格式 | 大小 | 元数据与可运行性 |
|---|---|---:|---|
| Qwen3-0.6B Q8_0 | `<model-root>\agentexpr-retained\qwen3-0.6b-20261023\Qwen3-0.6B-Q8_0.gguf`（GGUF） | 639,446,688 bytes | 有低资源 JSON 清单、`NATIVE_TOOL_CALLS`、context 1024；无 tokenizer/chat-template sidecar。 |
| Qwen3.5-4B Q4_K_M | `<model-root>\qwen3.5-4b\Qwen3.5-4B-Q4_K_M.gguf`（GGUF） | 2,740,937,888 bytes；SHA-256 `00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4` | 有 native-tool context 3072/4096 清单和一个 structured-intent 清单；无 tokenizer/chat-template sidecar。本轮选用 3072、batch 256、ubatch 64、8 threads 的现有 memory-safe profile。 |
| Qwen3.5-9B Q4_K_M | `<model-root>\qwen3.5-9b-bartowski\Qwen_Qwen3.5-9B-Q4_K_M.gguf`（GGUF） | 6,169,341,984 bytes；SHA-256 `D784CE9EDA1A5A7B51E8F705A9E6310844BF4F173654D115823C775FDEA56D43` | 有 3072/6144 `NATIVE_TOOL_CALLS` 清单；无 tokenizer/chat-template sidecar；本机可用内存下优先选 4B 作短迭代。 |
| Qwen2.5-Coder-3B | `<model-root>\qwen2.5-coder-3b` | 未找到权重文件 | 有配置/日志目录但没有可运行模型权重，未用于验收。 |

4B 服务运行时为 `<model-root>\qwen3.5-4b\.runtime\llama-b11424\llama-server.exe`，版本 `0.5.0-dev (build 11424, commit 6c59c4007)`，EXE SHA-256 `F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D`。Qwen 0.6B、9B 目录中的 b11424 运行时，以及 qwen2.5-coder / Agentexpr 留存的 b10964 运行时也都报告无可用设备。主机为 Intel i7-14650HX、16 cores/24 logical processors、16 GiB RAM；启动前可用 RAM 约 6.75 GB、可用提交额度约 11.9 GB。RTX 4060 Laptop GPU 有 8,188 MiB 显存、检查时约 6,932 MiB 可用，但当前运行时没有 GPU backend，故本次没有使用 GPU。

本地配置 `config/config.toml` 在此前不存在；本轮按已检查的 `[llm]` Schema 新建，使用 loopback OpenAI-compatible 地址和本地占位凭证。配置 SHA-256 为 `40B887F9ADAC2AA5DDB980CE58361D267C064664472336E1ED3C409D68ACB658`；`git check-ignore` 确认由 `config/.gitignore` 忽略，文件内容没有进入证据或 Git。项目 `.gitignore` 也检查并补充了模型权重、模型缓存、凭证和私有配置排除规则。

服务使用 `127.0.0.1:11434`、context 3072、8 threads、batch 256、ubatch 64、parallel 1、`--jinja`，未启用 llama-server 内置工具。启动约 3.6 秒后 `/health` 返回 HTTP 200；`/v1/models` 返回 HTTP 200，model alias 为 `devflow-qwen35-4b`，日志确认 Qwen3.5-4B GGUF 已载入，服务日志中的 n_ctx 为 3072。服务启动时有一次本地安全提示：未配置服务端 API key 且 CORS 允许所有来源；本次绑定仅为 IPv4 loopback，未开启其他地址。

唯一一次真实 `ask` 命令为：

```powershell
.\.venv\Scripts\python.exe -m devflow_agent.cli ask `
  --repo tests\fixtures\repos\readonly_demo `
  --question "Explain the total function and cite the source." `
  --max-steps 6 --timeout-seconds 180
```

CLI 运行 39.328 秒，进程退出码 3，状态 `failed`、终止原因为 `tool_error`。模型产生 2 次实际模型请求，均以 `finish_reason=tool_calls` 正常结束：第一次输入约 524 tokens、生成 138 tokens、服务端测得总计 19.506 秒，并调用 `repository_search_readonly`，找出 `src/totals.py` 与测试文件；第二次输入约 348 tokens、生成 163 tokens、总计 19.620 秒，并调用 `repository_readonly_read` 读取 `src/totals.py`。调用 ID、参数和原始返回保存在 JSON 轨迹中。

第二次调用请求 `start_line=1,end_line=20`。真实文件有 10 行，工具准确返回 `invalid_line_range` 和 `line_count=10`。因为运行合同对工具错误采取 fail-stop，第三次模型请求没有发生。结果没有可信读取证据，`answer` 为空，引用校验失败（`missing_path_line_citation`、`no_trusted_read_evidence`）。这证明本地模型服务和原生工具调用解析可以工作，也证明工具错误被拒绝；它没有证明读取后推理、最终回答或真实模型引用准确性。

合成 fixture 的三个文件在运行前后 SHA-256 完全一致；实际工具集合只有搜索和可信读取两个工具，未出现写入、任意代码执行、MCP 或审批调用。运行证据、模型请求计时及服务日志见 [本次 JSON 报告](../reports/public/M1_3_REAL_MODEL_ACCEPTANCE_2026-10-23.json)。
## 测试

M1.3 确定性套件在本轮先后执行两次：

- 第一次在本地配置仍位于项目路径时执行：38 项中 31 passed、6 failed、1 skipped，退出码 1。6 项失败均来自测试导入隔离器主动拒绝加载项目根目录中的 `config/config.toml`（`refusing to load a project-local config.toml`），不是功能断言失败。
- 第二次将本轮创建的本地配置临时移入本次证据目录，运行完成后以 `finally` 恢复；恢复前后配置 SHA-256 相同。结果为 **38 项运行、37 passed、1 skipped、0 failed，退出码 0**。跳过项是主机策略阻止创建真实目录 junction；解析层越界测试仍执行。

既有报告记录的 13 个 Python 源文件编译检查、CLI `--help` 和 M1.2/M1.3 专项测试结果保留为先前证据；本轮没有修改生产代码，也没有重建 EXE。真实模型验收单独计为一次 `ask`：模型请求 2 次、工具调用 2 次，但最终门禁失败，退出码 3。服务健康检查本身通过；模型服务启动不等于 Agent 验收通过。
## 安全复核状态

实现者对实际 Agent Loop、ToolCollection 构造、失败终止、可信读取引用验证、日志开关及合成仓库哈希测试做了第二遍源码复核。独立 Tester/Reviewer 未运行；本次没有提交或推送以触发外部审查，也未使用另一位审查者。因此这不计作独立复核通过，仍需在真实模型门禁前由独立审查者检查同一候选代码和本报告。

## 能力判定

| 能力 | 状态 | 依据 |
|---|---|---|
| 固定 OpenManus 实际 ToolCallAgent / ToolCollection 离线流程 | `VERIFIED`（M1.2 范围） | 确定性响应完成搜索→读取→引用回答；本轮 37 项确定性测试通过、1 项跳过。 |
| 本地模型服务加载与健康 | `VERIFIED`（服务层） | 本机 Qwen3.5-4B 已载入；`/health` 与 `/v1/models` 均返回 HTTP 200；不代表 Agent 流程通过。 |
| 真实模型自主选择工具 | `PARTIAL` | 真实模型先选择搜索、再选择可信读取，生成合法原生 `tool_calls`；读取调用的行范围参数越界。 |
| 根据可信读取结果继续推理 | `NOT_VERIFIED` | 没有成功读取源码，因此没有读取证据返回模型，也没有后续推理请求。 |
| 真实回答引用准确性 | `NOT_VERIFIED` | 本次没有最终答案或成功读取引用。 |
| 只读工具边界 | `VERIFIED`（确定性与本次观测范围） | 活跃集合只有搜索、可信读取；越界读取被拒绝，fixture 哈希未变化。 |
| 写入、人工审批、修复、失败后重规划 | `NOT_RUN` | 不在 M1.3 范围内；没有写入工具，本轮没有审批或 Task B。 |
| GPU 推理 | `BLOCKED`（现有运行时） | RTX 4060 可用，但所有已发现 llama.cpp 运行时的 `--list-devices` 均为 `(none)`；本轮 CPU 运行。 |
## 下一步最小解除条件

先改进可信读取工具的 schema/说明，让模型知道行号必须落在文件范围内，并让搜索结果提供可用的行数信息或使安全的读取范围可以省略；服务端仍须对实际读取字节和范围做严格校验，不能静默夹取或接受越界。加入确定性回归后，再为新的单次真实验收创建独立运行目录。只有模型成功读取源码、读取结果进入下一次模型请求并产生正确 `src/totals.py:4-6` 引用，才可将 M1.3 标为 `REAL_MODEL_READONLY_AGENT_VERIFIED`。

当前配置和模型仍在本机，配置被 Git 忽略；服务已退出。本轮没有下载模型、调用外部 API、写入合成仓库、运行 Task B、提交或推送。
