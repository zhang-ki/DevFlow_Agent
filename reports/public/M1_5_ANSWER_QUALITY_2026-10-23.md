# M1.5：答案质量门禁与固定评测集

日期：2026-10-23
总体状态：PARTIAL
确定性答案质量门禁：PASS
真实模型原始验收：FAIL（保留原始 ask 退出码 3）；保存回答的离线重新评估：PASS

## 本轮实现

新增 devflow_agent/answer_quality.py：以 Python AST 白名单求值，不使用 eval 或 exec。支持数字、括号、一元正负号、加减乘除、整除、取模、幂运算和只接受字面量数字列表的 sum。输入、表达式长度、AST 节点/深度、指数、列表长度和结果大小均有限制；浮点比较使用 1e-9 容差。除可安全解释的语法外，其余计算返回 UNVERIFIABLE。检查器只认出答案中明确陈述的有限算式，不覆盖任意自然语言数学或事实。

答案质量状态区分 PASS、NUMERIC_MISMATCH、INVALID_CITATION、MISSING_EVIDENCE、UNVERIFIABLE、MODEL_OR_TOOL_FAILURE；无数值要求的普通回答的数值组件为 NOT_APPLICABLE。PASS 仅表示执行轨迹、受限数值检查及引用来源检查满足门禁，不证明所有自然语言结论均由引用蕴含。

本轮离线修复了四种等价引用形式（普通/Markdown 路径、`lines` 和冒号范围），并为 `Expression`/`Result` 分段回答增加受限识别。任务明确要求数值时，零条可验证声明返回 UNVERIFIABLE；不要求数值时不因缺少算式而失败。原始模型记录未被修改。

源码引用复核要求引用属于当前授权工作区、行范围实际落在成功可信读取片段内，片段 SHA-256 与返回内容一致，且重新读取时完整文件 SHA-256 未变化。状态可区分行号/格式错误、未读取路径、过期文件、缺失证据及模型/工具失败。

检测到可确定的算术矛盾时，ask 最多沿用同一只读 OpenManus Agent 发起一次纠正请求；该请求只允许已有的搜索和可信读取工具。纠正预算使用 Agent 实际 current_step 剩余额度和原整轮剩余时间，不重置总截止。超预算或纠正后仍矛盾时，返回失败状态，不把不完整结果标为通过。

新增 evals/m1_5_answer_quality.jsonl，固定 10 个合成案例：源码证据、正负数算术、错误行号、过期证据、非法工具参数、路径穿越、不完整输出、无法确定结论和恢复预算耗尽等。固定输入/响应仅验证门禁本身，不代表模型能力。

## 确定性验证

| 检查 | 实际结果 |
|---|---|
| M1.5 答案质量专项 | 16 项通过，0 失败，退出码 0；使用项目 .venv。 |
| M1.3/M1.4/M1.5 合并回归 | 58 项运行、57 通过、1 跳过、0 失败，退出码 0；[最终日志](M1_5_FULL_REGRESSION_FINAL_2026-10-23.log)。 |
| 跳过项 | Windows 主机拒绝创建目录 junction；其他路径边界分支仍由确定性测试覆盖。 |
| Python 编译 | 4 个受影响 Python 文件通过，退出码 0。 |
| CLI 帮助 | 通过，退出码 0；[帮助日志](M1_5_CLI_HELP_2026-10-23.log)。 |
| 合成导入隔离回归 | 修为在新子进程核验适配器不导入 app.agent，避免测试收集顺序影响既有安全断言；专项通过。 |

在项目虚拟环境外的系统 Python 上曾有一次专项启动失败（11 项通过、1 项因未安装 openai 而错误）；后续所有最终专项和合并回归均使用项目 .venv，依赖未安装或变更。

完整回归在独立临时源码副本中运行，副本排除了本地 config/config.toml、.git 和原虚拟环境；原私有配置未读取、移动或复制。第一次完整回归曾因新测试模块在发现阶段导入 app.agent 而触发一项既有导入隔离断言失败。测试随后改为新进程验证该导入边界，最终完整回归通过；首次失败日志保留在 M1_5_FULL_REGRESSION_2026-10-23.log。

## 历史真实模型准备尝试（此前未发出 ask）

此前两次有界准备过程均在健康检查前失败，当时没有发出 ask；原始记录保留如下。本次经用户新授权执行的唯一真实 ask 见下一节。

1. 运行 ID 20261023T100921Z-m15-qwen35-4b：只读 profile 确认模型别名 devflow-qwen35-4b、地址 127.0.0.1:11434/v1 和 max_tokens=1024。Qwen3.5-4B Q4_K_M 权重 SHA-256 为 00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4；llama-server b11424 SHA-256 为 F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D。PowerShell 将 llama-server --version 的版本输出作为终止 NativeCommandError 处理，脚本在服务进程启动前中止。server PID、health、models 和 ask 均为 NOT_RUN。
2. 运行 ID 20261023T101326Z-m15-qwen35-4b-final：版本号改用与已核实二进制哈希匹配的历史记录后，资源预检通过（可用内存 5.29 GiB，模型文件 2.55 GiB；采用的最低估算门槛 4.05 GiB），并启动本轮拥有的 llama-server PID 58312。服务 stderr 只确认开始载入 GGUF，尚未确认加载完成。PowerShell 在请求 /health 前无法解析 System.Net.Http.HttpClientHandler，health、models 和 ask 都未执行；模型调用为 0。实际脚本 SHA-256 为 E502C9ADC9270681071FA76A705A507FC5C720312ED8283FE7E5752FAA926FCE。

第二次运行的合成仓库在创建后完成哈希前后核对，两个文件均未变化。受控清理耗时 0.394 秒；本次拥有的 server 和其 conhost 后代 PID 63100 均已退出，11434 连接探测失败，未发现残留服务。原始证据分别保存在 reports/m1_5_real_model_runs/20261023T100921Z-m15-qwen35-4b/ 和 reports/m1_5_real_model_runs/20261023T101326Z-m15-qwen35-4b-final/。这两次错误属于验收脚本兼容性问题；不能据此判断 Qwen 能力、服务健康或模型加载成功。

配置文件只经现有安全读取器加载并仅输出别名、loopback 地址和输出上限；未显示凭证。模型和运行时均使用已有文件，无下载。
## 本次唯一真实验收：20261023T102601Z-m15-python-health

只新增验收脚本和脚本测试，未修改业务代码。Python 标准库 urllib.request 使用显式空 ProxyHandler，拒绝重定向；/health 有界轮询 75 秒，每次请求最多 2 秒。真实 HTTP 请求和响应由只记录正文的观察器保存，未修改模型返回、工具选择或传输内容，未记录认证头。

- Python：<repo-root>\.venv\Scripts\python.exe（Python 3.12.7）。
- 模型：<model-root>\qwen3.5-4b\Qwen3.5-4B-Q4_K_M.gguf；SHA-256 `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`。
- 运行时：<model-root>\qwen3.5-4b\.runtime\llama-b11424\llama-server.exe；SHA-256 `f8df891a5e4d10fa8cfc0f86c8d99617a06edd18d046e18b59ec3354f17fc12d`。
- 实际 --version：0.5.0-dev，build 11424，commit 6c59c4007；退出码 0，耗时 0.047 秒，版本输出来自 stderr 并已保存。
- 模型服务：CPU，127.0.0.1:11434，context=3072，max_tokens=1024，单请求上限 120 秒，ask 整轮 180 秒、最多 6 步。未改变私有配置。
- 资源预检：启动时可用内存 5.33 GiB；没有预存模型服务或目标端口监听。
- /health：4 次有界探测，3.063 秒后 HTTP 200、status=ok；/v1/models HTTP 200、ID=devflow-qwen35-4b。实际监听 127.0.0.1:11434 的 PID=65408，与本轮服务一致。

| 模型请求 | 输入/输出 tokens | 耗时 | HTTP / 结束原因 | 实际行为 |
|---|---|---|---|---|
| 1 | 887 / 119 | 23.437 秒 | 200 / tool_calls | 自主 repository_search_readonly，query=total signed amount sum |
| 2 | 1269 / 96 | 14.516 秒 | 200 / tool_calls | 自主 repository_readonly_read，path=src/totals.py，省略范围使用有界默认读取 |
| 3 | 1646 / 509 | 57.172 秒 | 200 / stop | 读取结果进入该请求后形成完整答案 |

实际只运行一次 ask、共 3 次模型请求、2 次只读工具调用。Agent 耗时 95.281 秒，外层 CLI 98.002 秒，退出码 3。两个原生调用 ID 的结果都恰好出现一次，并与下一请求的工具消息及 CLI 工具轨迹逐项一致；tool_trace_issues=[]。没有截断、超时或协议失败。

模型回答正确指出 total() 调用 sum()，计算步骤为 0→-100→-50→-250，最终给出 -250。源码只读结果含 src/totals.py 的第 1–3 行；完整文件和片段摘要均为 `2195deb70718b9bd198ac5caa3bc2f0c3816cd10b0f9e538eda0de7487267ea5`。

**实际失败点是答案格式解析，不是启动或模型计算：**

1. 模型输出的引用为 `` `src/totals.py` lines 1-3 ``。real_readonly_agent.py 的 _CITATION_RE 需要路径与 lines 直接相邻，只允许中间空白，未接受路径后的 Markdown 反引号，因此返回 missing_path_line_citation。离线对照证明同一真实读取证据下，纯文本 src/totals.py lines 1-3 通过，原 Markdown 形式失败；没有修改原始答案来制造通过。
2. 模型把 Expression、计算表和 Result 分段展示。answer_quality.py 的抽取规则只支持紧邻的 =/returns/is 等关系，因此 claims_checked=0。原检查器返回 PASS（零条矛盾），但本轮验收严格记为 **UNVERIFIABLE**，不能等同于数值已自动验证。独立复核确认答案显示的 -250 正确。
3. 整体原始状态为 **MISSING_EVIDENCE**，最终真实门禁为 **FAIL**。纠正次数 0，没有进入真实数值纠正路径。另有静态待回归点：客户端在可选纠正分支前关闭；这不是本次失败原因，本轮未更改该业务逻辑。

本轮脚本与质量专项：21 run / 21 passed / 0 failed / 0 skipped，退出码 0（5 项既有 HTTP 测试、16 项质量测试）。Python 编译通过，PowerShell 语法检查通过。此前完整回归 58 run / 57 passed / 1 skipped 的结果按历史记录保留；直接在含私有 config 的工作区发现运行会触发 9 项既有隔离保护，未将其计入功能失败。

清理：本轮模型 PID 65408、conhost 后代 PID 43004 均退出，ask PID 71920 正常结束且退出码 3。受控终止计时 0.628 秒，最终识别的自有进程残留 0，11434 端口关闭。两个合成文件前后 SHA-256 完全一致。

原始证据：[运行汇总](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/ask-run.json)、[实际回答](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/ask.stdout.json)、[真实请求](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/model-requests.jsonl)、[真实响应](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/model-responses.jsonl)、[离线定位](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/validator-diagnosis.json)、[清理记录](../m1_5_real_model_runs/20261023T102601Z-m15-python-health/process-cleanup.json)。

本轮新增：scripts/m1_5_http_support.py、scripts/verify_m1_5_local_acceptance.ps1、scripts/recheck_m15_saved_answer.py、tests/devflow_agent/test_m1_5_acceptance_http.py、devflow_agent/diagnostics.py、tests/devflow_agent/test_m2_diagnostics.py、evals/m2_ci_failures.jsonl 及合成诊断 fixture。更新本报告、JSON、路线图及 CLI 状态说明；此前报告快照保存在本运行目录 prior-* 中。没有安装依赖、改全局设置、提交、推送或执行写入工具。

## 能力结论与下一步

- 确定性算术校验、引用来源/行范围/摘要校验、质量状态分类和最多一次纠正路径：VERIFIED（确定性范围）。
- M1.5 固定评测集：10 个案例均命中预期状态。
- 本次真实 Qwen 自主搜索、可信读取、读取结果进入下一请求及完整最终回答：PASS。原始答案的旧自动门禁 FAIL，因解析器不支持 Markdown 引用且将零数值声明错误视为 PASS。随后使用 `scripts/recheck_m15_saved_answer.py` 对同一保存回答、同一工具轨迹和同一读取摘要做离线重新评估：引用 PASS、数值 PASS（claims_checked=1，-250）、整体 PASS；这不是新的模型运行，也不改变原始 ask 退出码 3。
- 完整自然语言事实蕴含：不在本验证器能力范围。
- 写入、审批和 Task B：NOT_RUN。

M1.5 总体保持 PARTIAL：真实运行本身仍以原始退出码 3 结束，离线修复已证明质量门禁可正确重评该回答，但没有新的真实模型运行来改变历史运行结论。不得篡改本次失败记录；本轮未再次启动模型。

M2 的只读 CI 失败诊断已实现为 `devflow_agent/diagnostics.py` 与 `cli diagnose`：解析固定 pytest 日志、调用现有搜索/可信读取工具、输出路径/行号/hash 证据和不确定性。新增 M2 专项 3 项全部通过，CLI 合成演示通过；一次真实 Qwen3.5-4B 尝试已记录模型多工具调用被安全拒绝的失败，详见 [M2 报告](M2_CI_DIAGNOSTICS_2026-10-23.md)。
## 文件变更

新增：devflow_agent/answer_quality.py、devflow_agent/diagnostics.py、evals/m1_5_answer_quality.jsonl、evals/m2_ci_failures.jsonl、tests/devflow_agent/test_answer_quality.py、tests/devflow_agent/test_m2_diagnostics.py、scripts/recheck_m15_saved_answer.py、tests/fixtures/repos/m2_ci_demo/、tests/fixtures/logs/m2_assertion_total.log、reports/packaging/M1_5_ANSWER_QUALITY_2026-10-23.md 和同名 JSON、reports/packaging/M2_CI_DIAGNOSTICS_2026-10-23.md 和同名 JSON。

修改：devflow_agent/real_readonly_agent.py、tests/devflow_agent/test_readonly_repository.py、docs/READ_ONLY_REPOSITORY_CLI.md、plans/ROADMAP.md。另保存最终回归/CLI 日志和两次真实模型门禁准备记录；没有生产写入工具、Task B、提交或推送。
