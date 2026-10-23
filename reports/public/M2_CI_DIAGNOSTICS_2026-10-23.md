# M2 CI failure diagnosis — bounded read-only slice

Date: 2026-10-23
Branch: `devflow/m0-openmanus`
HEAD: `3309bf4e416fb1c74b008f3e86494439a31bad53`

## Result

**PARTIAL.** The deterministic diagnosis slice is implemented and verified. It accepts a captured pytest log, parses bounded failure records, uses only the existing repository search and trusted-read tools, and emits structured JSON or Markdown with candidate root causes and evidence. The real Qwen diagnosis reached the tools but the final answer was truncated; the M2 real-model gate therefore remains unverified.

## Implemented scope

- `devflow_agent/diagnostics.py` parses `FAILED` records, traceback paths/lines, exception classes, bounded excerpts, and redacted messages.
- It reuses `RepositorySearchTool` and `RepositoryReadTool`; it never executes commands found in a log and never writes the authorized repository.
- Evidence includes relative path, line range, evidence ID, full-file SHA-256, fragment SHA-256, and a bounded summary.
- Root-cause classifications remain candidates with confidence and explicit uncertainty. Missing implementation frames or missing trusted reads produce `UNVERIFIABLE`/`PARTIAL`.
- `devflow_agent.cli diagnose` accepts an existing log and exit code and returns exit code `0` only for a report status of `PASS`; non-success states return `3`.
- `devflow_agent/openmanus_agent.py` now states the one-tool-per-decision contract explicitly to the model; the server-side multiple-call rejection remains fail-closed.
- `devflow_agent/openmanus_agent.py` now requires a compact final diagnosis contract (Summary, Root cause, at most three Evidence bullets with path/lines/SHA-256, Confidence, Unconfirmed) and forbids source/tool payload repetition.
- `DiagnosticReport.to_compact_dict()`/`to_compact_markdown()` provide the same bounded projection for offline reports; `devflow_agent.cli diagnose --compact` exposes it without changing the full report format.
- Fixed cases live in `evals/m2_ci_failures.jsonl`; the local demo fixture is `tests/fixtures/repos/m2_ci_demo` with `tests/fixtures/logs/m2_assertion_total.log`.

## Evidence and tests

The new M2 test module ran **3 tests, 3 passed, 0 failed, 0 skipped**, exit code `0`. It covers five fixed cases: assertion mismatch, type contract mismatch, missing/null value access, boundary index access, and a misleading log that correctly remains `PARTIAL`. It also checks bounded read-only behavior, path containment, and no fixture mutation.

The focused output-contract regression ran **5 tests, 5 passed, 0 failed, 0 skipped**, exit code `0`. It covers normal compact output, `model_truncated` fail-closed handling, invalid citations, missing evidence, and the three-item compact evidence cap. The isolated M3 patch-flow regression ran **5 tests, 5 passed, 0 failed, 0 skipped**, exit code `0`.

The M1.5 answer-quality regression ran **16 tests, 16 passed**, exit code `0`. The related repository and real-readonly-agent regression ran **35 tests, 34 passed, 0 failed, 1 skipped**, and the fixed ToolCollection integration ran **2 tests, 2 passed**, all with exit code `0`; the skipped case is the host policy preventing creation of a temporary directory junction. The combined affected suite was **56 tests, 55 passed, 0 failed, 1 skipped**, exit code `0`. Python compilation for five affected files passed with exit code `0`.

The local CLI demo completed with exit code `0` and status `PASS`. It parsed one failure, identified `src/totals.py` as the top-1 source candidate, returned two trusted evidence items, preserved `commands_executed: 0`, and reported `read_only_tools` as `repository_search_readonly` and `repository_readonly_read`. The synthetic repository was unchanged.

The real-model attempts used the existing Qwen3.5-4B Q4_K_M and llama.cpp b11424 on `127.0.0.1:11434` through the existing read-only `ask` loop over a synthetic `failure.log`; the deterministic `diagnose` parser itself is not yet exposed as a model tool. In `20261023T-m2-qwen35-4b-r2`, health and model identity passed, then three model calls emitted two searches followed by two reads in one decision. The one-tool safety contract rejected the incomplete pair (`tool_call_without_result` / `multiple_tool_calls_rejected`), returned exit code `3`, and produced no final answer or trusted read evidence. The fixture remained unchanged and the owned server plus descendants were cleared (`remaining_pids=0`, listener closed). The prior `r1` attempt is preserved as a harness argument-quoting failure (ask exit code `2`) and was not treated as model evidence.

After adding an explicit one-tool-per-decision instruction, the final bounded attempt `20261023T-m2-qwen35-4b-r3` completed two searches and two trusted reads. Both read results carried real file and fragment SHA-256 values, and the synthetic fixture stayed unchanged. The model then spent the remaining bounded output budget composing the answer and ended with `finish_reason=length`; the answer and citation were truncated, so the quality gate returned exit code `3` and no verified final diagnosis. The owned server and descendants were cleaned (`remaining_pids=0`, listener closed). This is a real model/tool trace, but it is not a passing M2 gate.

The r3 request evidence identifies the direct cause: the last request contained **2,131 prompt tokens**, generated **941 completion tokens**, and reported **3,072 total tokens**, exactly the configured llama.cpp context. The request allowed `max_tokens=1024`, but only 941 tokens remained after the prompt, so `finish_reason=length` is classified as `CONTEXT_EXHAUSTED_BY_OUTPUT`, not a missing tool result or a provider startup failure. The prompt/output contract was tightened after this run. The one permitted follow-up run `20261023T-m2-qwen35-4b-r4` did not start the model: its resource preflight returned `resource_preflight_insufficient_free_memory`, with zero model calls and owned-process cleanup complete. No further model retry was made.

The r4 preflight recorded approximately **0.30 GiB free RAM** against a **4.05 GiB minimum** (2.55 GiB model file plus the configured 1.5 GiB safety margin). This is why the run is `BLOCKED` before health, inference, or tool execution; it is not evidence of a new model or protocol failure.

## Bounded context-budget follow-up

The saved r3 evidence distinguishes the two limits. The llama.cpp stderr records `n_ctx_slot = 3072`, and the final response reports `prompt_tokens = 2131`, `completion_tokens = 941`, `total_tokens = 3072`, `max_tokens = 1024`, `finish_reason = length`; the server also records `truncated = 1`. Therefore the request consumed the full **3072-token context window** after producing 941 output tokens. It was not a request-timeout or a standalone 1024-token completion cap: the prompt plus generated output reached the server context ceiling.

The bounded implementation now supports an explicit M2-only output override. The checked-in/local profile default remains unchanged, while the M2 harness requests `max_output_tokens = 1536` and starts llama.cpp with `--ctx-size 4096`. The client enforces a hard 1536-token ceiling, preserves fail-closed truncation/citation handling, and records the effective budget in the run result. The runtime's `--help` preflight confirmed support for `--ctx-size` before any model launch.

The deterministic follow-up gate ran **23 tests, 23 passed, 0 failed**, exit code `0`; Python compilation passed, the runtime version check passed (`b11424`), and the CLI exposes the bounded output option. The single new bounded run record `20261023T-m2-qwen35-4b-r5-ctx4096` was intentionally **not started**: the final preflight measured **0.473 GiB free RAM** versus **4.053 GiB required** (2.553 GiB model file plus the existing 1.5 GiB safety margin). This is below the safe threshold for the larger context, so the run is recorded as `BLOCKED_RESOURCE_PREFLIGHT` with zero model calls and no new process. M2 remains **PARTIAL**; the saved real r3 response remains immutable evidence and is not reclassified.

The fixed evaluation aggregate is 5 cases: 4 evidence-backed `PASS` reports and 1 intentionally insufficient-evidence `PARTIAL` report. This is a deterministic localization metric, not a claim of causal proof or model quality.

## Security and limits

No Bash, Python execution, file editing, write, approval, MCP, network, or external API tool was registered or called. The parser does not run commands from logs. It reads only the explicit repository root and rejects evidence outside that root. Logs are bounded and secret-shaped values are redacted in excerpts and messages.

The implementation does not rerun pytest, prove causality, or perform failure-after-write replanning. M3 now has a separate diff-only proposal/approval primitive in `devflow_agent/patches.py`; it applies only to a distinct temporary synthetic copy and is not registered as an Agent tool. The real Qwen M2 gate is **NOT_VERIFIED** because the only permitted follow-up was blocked before model startup by the resource preflight. The deterministic compact-output and M3 safety gates are passing.

## Reproduction

~~~powershell
.\.venv\Scripts\python.exe -m devflow_agent.cli diagnose --repo tests/fixtures/repos/m2_ci_demo --log tests/fixtures/logs/m2_assertion_total.log --exit-code 1 --format json
.\.venv\Scripts\python.exe -m devflow_agent.cli diagnose --repo tests/fixtures/repos/m2_ci_demo --log tests/fixtures/logs/m2_assertion_total.log --exit-code 1 --format markdown --compact
.\.venv\Scripts\python.exe -m unittest tests.devflow_agent.test_m2_diagnostics -v
\.venv\Scripts\python.exe scripts/demo_m3_repair.py --approve
~~~

## Latest bounded real-model run — 20261023T-m2-qwen35-4b-r6-ctx4096

The one permitted 4096-context follow-up was executed once with the existing Qwen3.5-4B Q4_K_M and llama.cpp b11424. The runtime version check passed, `/health` reached HTTP 200 after three bounded loading responses, `/v1/models` returned the expected `devflow-qwen35-4b` identity, and the final model call used `finish_reason=stop` rather than truncation. The model made one repository search followed by two trusted reads; all tool-call IDs were paired and two read-evidence records were retained. The answer cited `src/totals.py` lines 1–2 and `tests/test_totals.py` lines 3–4 with matching SHA-256 values.

The acceptance command exited `1` and the saved run record reports `answer_quality=UNVERIFIABLE`: citation and execution checks passed, but the bounded numeric checker deliberately cannot evaluate the project-specific function call `total([1, -1])` and therefore fails closed. This is a quality-gate limitation for this diagnostic prompt, not a model truncation or startup failure. The saved run record's `ask_exit_code` field is null; the outer acceptance exit code is preserved as `1` rather than inferred. The synthetic fixture hashes are unchanged, and owned model processes reached zero with the listener closed after `0.546` seconds.

M2 remains **PARTIAL / NOT_VERIFIED**. No second real model run was started. Evidence: [`reports/m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/m2-real-model-run.json`](../m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/m2-real-model-run.json).

## Offline numeric-applicability follow-up

`devflow_agent/answer_quality.py` now distinguishes `SUPPORTED`, `STATICALLY_VERIFIABLE`, `UNSUPPORTED`, `UNVERIFIABLE`, and `NOT_APPLICABLE`. Static verification is intentionally limited to a trusted Python read containing exactly one simple function with a single side-effect-free return expression. Literal arguments, closed parameter names, bounded arithmetic, and `sum(sequence)` are supported; branches, loops, dynamic calls, external state, ambiguous sources, missing evidence, and invalid evidence remain fail-closed. No `eval`, `exec`, or project-module import is used.

The new deterministic regression ran **8 tests, 8 passed, 0 failed, 0 skipped**, exit code `0`. It covers supported arithmetic, correct and incorrect `total([-100, 50, -200])`, a scalar helper, branch/dynamic/external functions, missing evidence, no numeric claims, source-expectation context, and stale trusted citations.

The preserved answer from `20261023T-m2-qwen35-4b-r6-ctx4096` was re-evaluated offline by [`scripts/recheck_m2_saved_answer.py`](../../scripts/recheck_m2_saved_answer.py). Citation validation remains `PASS`; the numeric result remains `UNVERIFIABLE` because the cited line says the test *expects* `total([-1, 1]) == 1`, which is not treated as a runtime behavior claim. The new result is saved at [`reports/m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/offline-quality-reassessment-static-numeric.json`](../m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/offline-quality-reassessment-static-numeric.json). The original model output, tool trace, and historical exit status were not modified; no model was restarted.

M2 remains **PARTIAL / NOT_VERIFIED**.

## 2026-10-24 follow-up

M2 remains **PARTIAL / NOT_VERIFIED**. Offline re-evaluation of the preserved Qwen run confirms that source/test citations pass, but diagnostic quality remains `UNVERIFIABLE`: the answer did not include the observed pytest actual/expected result, and the test expectation alone is not treated as runtime evidence. Historical run artifacts were not changed.

The focused M2 suite passed 59/59; the full isolated deterministic suite passed 126/128 with 2 skipped and 0 failed. Pre-commit, compile, PowerShell syntax, and diff checks passed. One fresh attempt (`20261024T-m2-qwen35-4b-r7-diagnostic-evidence`) was blocked during model-config preflight after the runtime version check. The model server and health/model requests were not run, and the underlying transient preflight cause remains unresolved. No new real-model diagnosis was produced.

## Final preflight hardening and bounded follow-up

The former runner used an inline `python -c` probe and converted every nonzero
result to `model_config_preflight_failed` without retaining stderr or a safe
exception record. That observability defect is confirmed; the exact transient
exception in the historical r7 run is not recoverable from its saved evidence.
The runner now calls a dedicated profile-preflight mode. Its record is limited
to stage, status, exception type, stable error code, sanitized diagnostic, and
allow-listed non-secret profile fields. It never copies an unexpected exception
message or traceback. Deterministic success, missing-config, malformed-TOML,
and unexpected-exception paths pass, and the actual read-only profile preflight
passed for `devflow-qwen35-4b` at the loopback endpoint.

The only permitted follow-up, run
`20261024T013100Z-m2-qwen35-4b-r8-preflight-fix`, passed the runtime version,
runtime capability, model/runtime hashes, and model-profile preflight. It then
stopped at `synthetic_pytest_baseline_did_not_match_expected_failure` before
starting llama.cpp. The captured pytest result was a real expected failure and
contained `E       assert 4 == 3`; the harness required the different text
`AssertionError: assert 4 == 3`. Health, model identity, inference, and tool
calls are therefore `NOT_RUN`; model calls are zero. Cleanup recorded zero
remaining owned processes and no listener. The runner had not yet created its
before-hash checkpoint, so this run cannot use its `fixture_unchanged` field as
an immutability proof.

M2 remains **PARTIAL / NOT_VERIFIED**. No retry was made. The next work, if the
project is reopened, is the isolated correction and deterministic validation of
that pytest-output matcher before any separately authorized model run.

Final local verification after the preflight change: the focused M2 suite ran
67 tests with 67 passes, 0 failures, and 0 skips (exit code 0). The complete
private-config-free source snapshot ran 136 tests with 134 passes, 0 failures,
and 2 host-capability skips (exit code 0). The skips are the existing link and
junction creation cases. Python compilation, PowerShell parsing, public JSON
parsing, `git diff --check`, and the final all-files pre-commit run all exited
0. M3 approval/one-time consumption, M4 report replay, and M5 bounded-replan
safety tests are included in that complete suite.

The pytest matcher defect was then corrected offline without another model
start. The helper now requires all four pieces of evidence: exit code 1, the
exact expected test node ID, the expected observed assertion line, and the
pytest failure summary. It accepts Windows path separators only after
normalization, rejects logs outside the run directory, limits the log to 1 MiB,
and records only booleans plus the log SHA-256. Four matcher tests cover the
preserved Windows output, missing node ID, wrong exit/assertion, and path escape.
The preserved r8 `baseline-pytest.log` now passes this read-only check. M2 still
remains **PARTIAL / NOT_VERIFIED** because that historical run stopped before
model startup and no further real-model run was made.
