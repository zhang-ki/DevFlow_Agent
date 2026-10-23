# Read-only repository analysis CLI

Date: 2026-10-23
Status: PARTIAL — the deterministic repository slice and bounded real-local-model entry point are implemented. The historical M1.3 run is recorded separately; the M1.5 follow-up reached the model and failed its answer-quality gate.

## Scope

This prototype is a CLI for an explicitly selected local source repository. It indexes supported text paths and Python class/function symbols in memory, performs stable keyword/path/symbol search, and returns bounded file excerpts with hashes derived from the bytes actually read. The `index`, `search`, and `read` commands are offline; `ask` uses the fixed OpenManus Agent Loop and is restricted to an explicitly configured loopback model endpoint.

The `ask` command can call a real local OpenAI-compatible model, but only after an explicit `config/config.toml` is present and its endpoint is verified as loopback. It refuses to fall back to the hosted endpoint in `config.example.toml`. No command executes repository code, creates or edits files in the authorized repository, runs tests inside it, or persists an index. Each CLI invocation builds a fresh in-memory index.

## Modules and call path

- devflow_agent/repository.py: path normalization and containment, exclusions, size and scan limits, AST symbol extraction, deterministic search, structured errors, and read evidence.
- devflow_agent/cli.py: index, search, and read subcommands; JSON output; non-zero structured errors.
- devflow_agent/real_readonly_agent.py: loopback-only model config check, proxy-free bounded client, tool-call/result pairing, and final citation validation against trusted reads.
- devflow_agent/openmanus_agent.py: bounded subclass of the fixed OpenManus `ToolCallAgent`; only one tool call is accepted per model decision and errors/truncation terminate the run.
- devflow_agent/tools.py: two low-dependency async adapters for search and read. Registration is allowed only into a collection that exposes an empty current tool set. It never imports app.* and refuses a non-empty collection, preventing accidental addition alongside Bash, PythonExecute, or editing tools.
- devflow_agent/openmanus_tools.py: an explicit factory adapter that accepts the OpenManus ToolCollection class, constructs it with no default tools, and verifies the exact two-tool allowlist and schemas.
- tests/devflow_agent/test_readonly_repository.py: unittest coverage.
- tests/devflow_agent/test_openmanus_tool_collection_integration.py: integration coverage against the fixed OpenManus ToolCollection source.
- tests/fixtures/repos/readonly_demo/: synthetic Python repository copied into temporary directories by tests.

CLI path:

~~~mermaid
flowchart LR
  CLI[devflow_agent.cli] --> WS[AuthorizedWorkspace]
  WS --> IDX[build_index]
  IDX --> AST[Python ast symbols]
  IDX --> SEARCH[RepositoryIndex.search]
  WS --> READ[read_text]
  READ --> HASH[full file and excerpt SHA-256]
  SEARCH --> CANDIDATE[index candidate, not read evidence]
  READ --> EVIDENCE[trusted file read evidence]
  CLI --> LIVE[ask: local-only OpenManus loop]
  LIVE --> TOOLS[exact two-tool allowlist]
  LIVE --> CITE[verify citations against read evidence]
~~~

Search results are candidates only. They include a relative path, candidate line locations, and the digest observed while building that in-memory index. They do not establish that a model read the file. The read operation produces a separate evidence ID, workspace ID, relative path, line range, exact content, full-file SHA-256, and fragment SHA-256. Optional task_id and run_id fields allow a future caller to associate the read with its own execution.

The full-file digest is calculated from the raw bytes. The fragment digest is calculated from the exact UTF-8 bytes corresponding to the returned lines. Reads re-resolve and re-read the file before issuing evidence; a changed path, metadata, content, or supplied indexed digest is rejected as stale.

## Boundaries and limits

The default limits are:

- 1 MiB per file.
- 10,000 scanned files.
- 64 MiB total indexed source bytes.
- 500 lines per explicit read.
- 65,536 UTF-8 bytes per returned result.
- 100 search results.

The scanner does not follow symbolic links or Windows junctions. Explicit reads resolve the target and reject paths outside the selected root. Absolute paths and parent traversal are rejected. Supported extensions are an allowlist of text/source formats; NUL-containing, non-UTF-8, binary, and oversized files are not indexed or returned.

The default exclusions include VCS metadata, virtual environments, build/cache directories, model and weight directories, local config/credential names, databases, executables, libraries, and model-weight formats. The exact patterns are in devflow_agent/repository.py.

The adapters implement the structural contract used by OpenManus ToolCollection: name, description, JSON-schema parameters, async call, and to_param(). `create_openmanus_readonly_collection` accepts an explicit collection factory, rejects non-OpenManus collection types and nonempty collections, then verifies that only the two exact adapter classes and schemas are present. M1.2 verified the normal fixed-version imports and actual `ToolCallAgent.run()` using deterministic model responses. The live entry point still requires the same exact two-tool collection. No Bash, PythonExecute, file editor, MCP, or external write tool is registered.

The live entry point refuses non-loopback model URLs, bypasses inherited HTTP proxy settings for its local client, makes one provider attempt per Agent decision, allows at most 6 decisions by default (hard maximum 12), uses a 180-second whole-run timeout (hard maximum 300 seconds), and caps output at 1024 tokens. It requires a complete provider finish reason before accepting a tool call, stops on tool errors, and accepts a final answer only when every path/line citation falls within an actual trusted file read. A conservative local byte-based token estimate avoids downloading `tiktoken` data; it is a budget estimate, not an exact model tokenizer.

## Commands

Run from the repository root with Python 3.12:

~~~powershell
.\.venv\Scripts\python.exe -m devflow_agent.cli --help
.\.venv\Scripts\python.exe -m devflow_agent.cli index --repo C:\work\sample-project
.\.venv\Scripts\python.exe -m devflow_agent.cli search --repo C:\work\sample-project --query "model startup" --path src --limit 10
.\.venv\Scripts\python.exe -m devflow_agent.cli search --repo C:\work\sample-project --query startup --symbol "Server.start"
.\.venv\Scripts\python.exe -m devflow_agent.cli read --repo C:\work\sample-project --path src\server.py --start-line 20 --end-line 45
.\.venv\Scripts\python.exe -m devflow_agent.cli ask --repo C:\work\sample-project --question "Explain the total logic and cite its source."
\.venv\Scripts\python.exe -m devflow_agent.cli diagnose --repo C:\work\sample-project --log C:\work\pytest.log --exit-code 1 --format markdown
~~~

The index command reports summary counts and a bounded list of indexed paths. It is not saved between processes. Search and read results are JSON. A search result has kind index_candidates_not_read_evidence; a read result has kind trusted_file_read. Passing --expected-sha256 to read rejects a candidate if the file changed after indexing.

The `diagnose` command consumes an already captured pytest log and exit code. It performs a bounded, read-only search and trusted read inside the explicitly selected repository, then emits candidate root causes, evidence paths, line ranges, and SHA-256 values. Commands found in the log are never executed. Evidence-backed locations are still reported as candidates with uncertainty when the log alone cannot prove causality.

Before `ask`, create `config/config.toml` from a local OpenAI-compatible provider configuration and set a loopback `base_url` (for example `http://localhost:11434/v1`). The CLI never uses example configurations for live requests and rejects hosted endpoints. The current workspace has no `config/config.toml`; therefore the real-model gate has not run. `ask` outputs the bounded tool trace, verified evidence metadata, answer, and citation-check result as JSON. Redirect stdout only if a durable trace file is needed.

Run the focused deterministic suite:

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests\devflow_agent -v
~~~

No dependency was installed during M1.3. The project `.venv` already contains the minimal OpenManus import chain recorded in [M1.2](M1_2_OFFLINE_AGENT_LOOP.md). The upstream full test suite was not run.

## Verification on 2026-10-23 (M1.3)

- Focused suite: 38 tests run, 37 passed, 1 skipped, exit code 0. The skipped test is OS junction creation blocked by host policy; resolver escape handling passed deterministically.
- Tests cover M1.2's fixed real OpenManus loop with scripted responses, the exact two-tool registry, trusted-read hash and line evidence, path-boundary rejection, result pairing, citation validation, local endpoint restrictions, bounded single-attempt behavior, finish-reason propagation, truncation rejection, and file logging disabled in local mode.
- All synthetic repository hash checks remained unchanged. A syntax compilation check passed for 13 changed/new Python files.
- CLI help includes `ask`. A real CLI invocation returned structured `local_model_config_missing` and exit code 3 before importing/creating a model client or making a request.
- Baseline before M1.3 changes: 22 run, 21 passed, 1 skipped. Current combined suite includes those regressions plus M1.3 deterministic tests.
- No real model/API request, local model service startup, Task B, write, approval, upstream full test, or commit/push occurred.

## Known limitations and next step

The index is in-memory and rebuilt for every command or tool call; there is no incremental cache, import/call graph, ranking evaluation, or citation UI. Python AST extraction indexes classes and functions, not every variable or cross-file relationship. OS-level junction creation remains skipped. The historical M1.3 run was PARTIAL because the model selected an invalid range; the M1.5 follow-up verified the local endpoint and real search/read flow but exposed answer-format coverage gaps. The code's conservative byte token estimate is not equivalent to a provider tokenizer, and general natural-language entailment remains NOT_VERIFIED. M2 compact reports are available with `diagnose --compact`; M3's diff/approval primitive is intentionally outside the read-only ToolCollection and only applies to a separate synthetic copy.

## M1.5 答案质量门禁（2026-10-23）

ask 现在会对明确陈述且属于受限算术语法的结果做确定性 AST 校验，并将数值、执行/工具轨迹和可信源码引用分开报告。支持数字、括号、正负号、基本算术、整除、取模、幂运算和字面量数字列表 sum；不支持的表达式标为 UNVERIFIABLE。该检查不证明任意自然语言事实。

引用只有在路径属于授权工作区、行号被成功可信读取覆盖、片段摘要匹配且当前完整文件摘要未变化时才有效。数值不一致时 ask 最多进行一次受原步骤和整轮时间预算约束的纠正；重试仍不一致或预算耗尽时失败。可见状态包括 PASS、NUMERIC_MISMATCH、INVALID_CITATION、MISSING_EVIDENCE、UNVERIFIABLE 和 MODEL_OR_TOOL_FAILURE。

固定评测集位于 evals/m1_5_answer_quality.jsonl，包含 10 个确定性案例。M1.5 历史确定性门禁为 58 项运行、57 通过、1 跳过、0 失败；跳过项是当前 Windows 主机不允许创建目录 junction。保存的真实 Qwen 回答原始退出码仍为 3；修复后使用同一回答和可信读取证据完成离线重评为 PASS，但这不是新的模型运行，因此 M1.5 总体仍为 PARTIAL。详见 [M1.5 综合报告](../reports/public/M1_5_ANSWER_QUALITY_2026-10-23.md) 和 [机器可读报告](../reports/public/M1_5_ANSWER_QUALITY_2026-10-23.json)。

### M1.5 真实验收续跑（2026-10-23）

运行 20261023T102601Z-m15-python-health 已通过本地健康检查与模型身份核对；真实 Qwen 用 3 次请求完成搜索、可信读取及最终回答，正确给出 -250。当前门禁不支持该回答的 Markdown 引用格式，分段算式也未被提取，因此 CLI 退出码 3、整体 MISSING_EVIDENCE，M1.5 保持 PARTIAL。不能把零条已提取算式的 numeric PASS 当作计算已验证。模型和本轮后代已清理，工作区哈希未变。详情见 [M1.5 报告](../reports/public/M1_5_ANSWER_QUALITY_2026-10-23.md)。

复现脚本为 scripts/verify_m1_5_local_acceptance.ps1；健康检查由 scripts/m1_5_http_support.py 的标准库 urllib 执行，明确禁用代理且先检查实际运行时版本。脚本只接受新 RunId，真实运行有一次 ask 上限。本次已结束，不自动重复启动。

## M2 只读 CI 失败诊断（2026-10-23）

`devflow_agent/diagnostics.py` 提供一个有界的 pytest 日志诊断切片。它解析失败测试、异常类型和 traceback 位置，复用 `repository_search_readonly` 与 `repository_readonly_read` 取得真实源码证据，并输出候选根因、置信程度、相对路径、行号和完整/片段 SHA-256。日志中的命令不会执行，报告明确标记 `read_only=true` 和 `commands_executed=false`。不具备实现帧、读取证据或因果复现条件时，报告会保留 `UNVERIFIABLE` 或 `PARTIAL`，不会把模型推测升级为事实。

固定评测集位于 `evals/m2_ci_failures.jsonl`，覆盖断言、类型、空值、边界和误导性日志五类案例。演示命令：

~~~powershell
.\.venv\Scripts\python.exe -m devflow_agent.cli diagnose --repo tests/fixtures/repos/m2_ci_demo --log tests/fixtures/logs/m2_assertion_total.log --exit-code 1 --format json
~~~

当前 M2 确定性切片已通过新增测试和本地 CLI 演示；真实 Qwen3.5-4B 诊断尝试已通过健康检查、完成搜索和可信读取，但最终回答在有界输出预算内被截断，引用门禁未通过。代码写入、补丁审批、隔离测试和失败后重规划不属于本阶段。
