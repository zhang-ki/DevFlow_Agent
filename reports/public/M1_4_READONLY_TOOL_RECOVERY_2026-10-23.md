# M1.4：可靠的只读工具调用与错误恢复

日期：2026-10-23
总体状态：PARTIAL
确定性门禁：PASS
真实模型闭环：FAIL（真实读取成功；最终答案因算术错误未通过）

## 结论

M1.3 的直接阻塞得到证实：搜索结果没有把索引中已计算的真实 line_count 交给模型；读取工具又要求模型自行猜测行范围；越界错误虽然按原生调用 ID 返回，但本地 Agent 适配器把所有工具错误都立即终止，模型没有机会修正。

M1.4 为搜索结果增加真实行数、允许有界默认读取、保留显式非法范围拒绝，并给 invalid_line_range 提供一次受限恢复。确定性流程验证了错误结果按原调用 ID 返回、Agent 观察错误并纠正；重复错误会安全终止。

唯一一次真实 Qwen3.5-4B ask 完成搜索、两次可信读取和最终回答。其路径/行号引用经本轮窄范围的解析器兼容修复后可与读取证据匹配，但模型回答中的算术示例错误：它称 sum([-100, 50, -200]) 为 -350，实际是 -250。因此真实问答未通过，不能标记 REAL_MODEL_READONLY_AGENT_VERIFIED，也没有重复调用模型。

## 故障定位与改动

| 范围 | 证据与改动 |
|---|---|
| 搜索行数 | devflow_agent/repository.py 的文件记录已从实际字节计算行数；M1.3 的 SearchHit/序列化结果未暴露该值。现在搜索项带有 line_count 与 line_count_source=fresh_index_snapshot，仍明确标为候选而非已读证据。 |
| 读取契约 | M1.3 读取 Schema 将 path/start_line/end_line 全部设为必填且缺少清楚的行号约束。现在只有 path 必填；显式范围仍采用 1 基、闭区间并严格验证，省略范围时从第 1 行提供最多 500 行、64 KiB 的有界预览。返回实际行数、实际范围、截断标记及文件摘要。 |
| 错误恢复 | 固定版本 OpenManus ToolCallAgent.act() 将工具结果作为带原 tool_call_id 和名称的 tool message 追加。M1.3 本地 execute_tool() 对任何错误设置终态，导致该消息没有被下一次模型调用观察。现在只将带结构化标志的 invalid_line_range 视为可恢复错误，每次 ask 最多恢复一次；其他错误及超限错误都明确终止。 |
| 协议完整性 | Agent 仍每个决策最多执行一个原生工具调用；缺失、重复或复用的调用 ID 不执行，工具结果必须匹配调用 ID 和工具名。收集器拒绝缺失或冲突的配对。 |
| 引用校验 | 真实模型给出 src/billing.py lines 3-7，旧校验器只接受 src/billing.py:3-7，错误地报告缺少引用。解析器现在同时接受两种语法，但仍要求路径和整段行号处于成功可信读取范围，并把完整文件 SHA-256 附在引用上。新增确定性回归。 |
| 安全边界 | 路径根限制、显式范围拒绝、读取前后文件状态核验、全文/片段 SHA-256、搜索候选不等同于读取证据、工具白名单均保留。运行时仅注册 repository_search_readonly 与 repository_readonly_read。 |

M1.3 首次失败的调用 ID 为 1hFke2zd03M4CDqlhKDIXb4xqnC5dE1u：工具对请求的 1–20 行返回 invalid_line_range 与实际 line_count=10。结果与调用 ID 配对完整，但 Agent 随即转为 FINISHED。代码路径与真实轨迹共同证明，当时缺少的是错误后恢复，而不是错误结果配对。

本轮真实运行没有触发错误恢复：Schema 允许省略行范围，模型第一次读取就请求了有界默认预览。错误恢复能力由确定性 OpenManus Loop 测试验证，不能据此声称真实 Qwen 已经在错误后成功自我修正。

## 真实模型运行

- 运行 ID：20261023T091935Z-m14-qwen35-4b
- 模型：Qwen3.5-4B Q4_K_M，<model-root>\qwen3.5-4b\Qwen3.5-4B-Q4_K_M.gguf
- 模型 SHA-256：00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4
- llama.cpp：b11424，llama-server.exe SHA-256：F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D
- 配置模型别名：devflow-qwen35-4b；端点仅绑定 127.0.0.1:11434；上下文 3072；本机现有运行时没有 GPU backend，本次使用 CPU。
- 修正后的服务器 /health 与 /v1/models 均返回 HTTP 200，模型列表 ID 与别名均为 devflow-qwen35-4b，服务日志确认加载指定 GGUF。
- ask 实际耗时 127.449 秒（Agent 运行计时 124.969 秒），4 次模型请求，均完整结束；CLI 退出码 3，原始状态 failed，原因为 citation_validation_failed。
- 实际工具轨迹：
  1. 搜索调用 rJwSnq3MmurW05AXC3BwfqJvwZBHI3d7：返回 src/billing.py，实际 line_count=9，候选标记为 read_evidence=false。
  2. 读取调用 MoCxQ796y7pmJ4jHuoW3qOEjA1PgeV1Z：省略范围，返回 1–9 行的有界可信读取。
  3. 读取调用 CLoZZkRRDlDSWgCjZyaJw6nnvTVnFufX：显式复读 1–9 行。
- 两次读取的完整文件及片段 SHA-256 均为 966da9b302980baee1496b6b731875286b786c98f01e24c821c2646955d79108，工具调用与结果均配对，tool_trace_issues=[]。读取后的真实源码范围为 src/billing.py:1-9。
- 保存的回答引用 src/billing.py lines 3-7；离线用修正后的引用解析器复核后，路径、行范围和文件 SHA-256 均匹配读取证据。此复核没有重新调用模型，也没有改写原始运行结果。
- 回答错误：模型举例声称 sum([-100, 50, -200]) == -350；正确值为 -250。这是内容正确性失败，故即使引用可核对，整次真实问答仍不合格。
- 合成仓库三个文件前后 SHA-256 完全相同；未写入工作区。最终真实模型状态为 PARTIAL/FAIL。

第一次仅启动服务的检查漏传了历史启动记录中的 --alias，服务虽健康但别名门禁失败，因此没有执行 ask。该服务器已受控退出；随后按已有历史启动参数进行一次正确启动，并且只执行上面记录的一次真实 ask。

## 测试与清理

| 检查 | 实际结果 |
|---|---|
| unittest discover -s tests/devflow_agent -v | 46 项运行，45 通过、1 跳过、0 失败，退出码 0；日志：[回归日志](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/m1_4_deterministic_regression.log)。跳过项为主机不允许创建临时目录 junction。 |
| 引用解析器专项 | 14 项运行、14 通过，退出码 0。 |
| python -m compileall -q devflow_agent | 通过，退出码 0。 |
| CLI --help | 通过，退出码 0；[帮助输出](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/cli-help.log)。 |
| 已保存真实答案的离线复核 | 新解析器认出引用且验证其范围和 SHA；独立算术核对失败。未发起模型请求。 |
| 本机配置完整性 | 测试时按保护流程临时移出配置路径后恢复；前后 SHA-256 均为 40B887F9ADAC2AA5DDB980CE58361D267C064664472336E1ED3C409D68ACB658。 |

一次真实 ask 的原始结果、请求摘要及回答见 [ask.stdout.json](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/ask.stdout.json)、[ask-run.json](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/ask-run.json)、[后验引用与内容检查](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/posthoc-validation.json) 和 [模型服务日志](../m1_4_real_model_runs/20261023T091935Z-m14-qwen35-4b/llama-server-corrected.stderr.log)。运行输出显示 4 次模型调用、3 个工具调用、2 个成功读取；真实工具错误恢复次数为 0。

服务器由本任务启动并在结束时受控终止，清理耗时 1.054 秒，退出码 -1（主动终止）；其 conhost.exe 后代无残留，11434 监听已消失。本次没有发现仍存活的本任务 Python ask 进程。启动别名未匹配的第一次服务清理耗时约 1.079 秒，也无残留。

## 能力结论与限制

| 能力 | 状态 |
|---|---|
| 搜索返回真实行数、可信读取和有界默认范围 | VERIFIED（确定性测试及本次真实调用） |
| 显式非法范围拒绝、路径边界、文件变化与摘要合同 | VERIFIED（确定性测试） |
| 原生调用 ID 配对、一次性错误恢复及预算耗尽安全失败 | VERIFIED（真实 OpenManus Agent Loop 的确定性测试） |
| 真实 Qwen 搜索后读取并根据读取内容回答 | PARTIAL（实际发生；答案有一项算术错误） |
| 真实模型遇到读取错误后恢复 | NOT_VERIFIED（本次真实模型未触发错误；仅确定性 Loop 测试覆盖） |
| 真实模型回答正确性与引用门禁 | FAIL（原始 CLI 退出码 3；后验引用匹配通过，但算术核对不通过） |
| 写入、审批、Task B、自主修复 | NOT_RUN |

下一项最小工作：改进最终答案的事实/算术检查或明确要求模型只概括有证据支持的行为；在新运行 ID 与新合成任务下重新执行真实模型门禁。不得重用本次运行来宣称下一次门禁通过。

个人配置不进证据正文。没有下载模型、安装完整 requirements.txt、运行 Task B、执行代码写入、审批、提交或推送。Git 分支和 HEAD 保持固定基线；本轮与已有项目改动继续保留在工作区。
