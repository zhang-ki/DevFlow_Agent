[CmdletBinding()]
param(
    [ValidateSet('check', 'm2', 'm3')]
    [string]$Demo = 'check',
    [string]$RunId = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$EvidenceRoot = Join-Path $ProjectRoot 'reports\interview_demo_runs'
$M2Evidence = Join-Path $ProjectRoot 'reports\m2_real_model_runs\20261023T-m2-qwen35-4b-r6-ctx4096'
$M3Script = Join-Path $ProjectRoot 'scripts\demo_m3_repair.py'

function Fail-Stage([string]$Code, [string]$Message) {
    Write-Error ("{0}: {1}" -f $Code, $Message)
    exit 3
}

function New-DemoRunId([string]$Kind) {
    return ("interview-{0}-{1}" -f $Kind, (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
}

function Write-JsonFile([string]$Path, [object]$Value) {
    $Value | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $Path -Encoding UTF8
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Fail-Stage 'PYTHON_NOT_FOUND' ("Project virtual environment is missing: {0}" -f $Python)
}

$required = @(
    (Join-Path $ProjectRoot 'devflow_agent'),
    (Join-Path $ProjectRoot 'scripts\demo_m3_repair.py'),
    (Join-Path $ProjectRoot 'scripts\run_m2_real_model_case.ps1'),
    (Join-Path $ProjectRoot 'config\config.toml')
)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) {
        Fail-Stage 'REQUIRED_FILE_MISSING' $path
    }
}

$importOutput = @(& $Python -c 'import devflow_agent' 2>&1)
$importExit = $LASTEXITCODE
if ($importExit -ne 0) {
    Fail-Stage 'IMPORT_CHECK_FAILED' (($importOutput -join "`n").Trim())
}

$listener = @(Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue)
$serviceState = [ordered]@{
    endpoint = 'http://127.0.0.1:11434'
    listener_present = ($listener.Count -gt 0)
    listener_pids = @($listener | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique)
    probe_performed = $false
    note = 'The launcher never starts or probes the model; it only reports the existing listener state.'
}

if ($Demo -eq 'check') {
    [ordered]@{
        status = 'READY_FOR_BOUNDED_DEMOS'
        project_root = $ProjectRoot
        python = $Python
        config_present = $true
        model_service = $serviceState
        m2_history = (Test-Path -LiteralPath (Join-Path $M2Evidence 'm2-real-model-run.json'))
        m3_script = (Test-Path -LiteralPath $M3Script)
        side_effects = @('no model start', 'no API request', 'no workspace write', 'no process termination')
    } | ConvertTo-Json -Depth 10
    exit 0
}

if ([string]::IsNullOrWhiteSpace($RunId)) {
    $RunId = New-DemoRunId $Demo
}
$RunDir = Join-Path $EvidenceRoot $RunId
if (Test-Path -LiteralPath $RunDir) {
    Fail-Stage 'RUN_ID_EXISTS' ("Refusing to overwrite existing evidence: {0}" -f $RunDir)
}
New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

if ($Demo -eq 'm2') {
    $sourcePath = Join-Path $M2Evidence 'm2-real-model-run.json'
    $offlinePath = Join-Path $M2Evidence 'offline-quality-reassessment-static-numeric.json'
    if (-not (Test-Path -LiteralPath $sourcePath) -or -not (Test-Path -LiteralPath $offlinePath)) {
        Fail-Stage 'M2_EVIDENCE_MISSING' 'The preserved M2 run or offline reassessment is unavailable.'
    }

    $source = Get-Content -Raw -LiteralPath $sourcePath | ConvertFrom-Json
    $offline = Get-Content -Raw -LiteralPath $offlinePath | ConvertFrom-Json
    $trace = @($source.tool_trace | ForEach-Object {
        [ordered]@{
            tool = $_.tool
            status = $_.status
            tool_call_id = $_.tool_call_id
        }
    })
    $summary = [ordered]@{
        demo_run_id = $RunId
        kind = 'preserved_real_model_trace_offline_replay'
        live_model_rerun = $false
        source_run_id = $source.run_id
        source_status = $source.status
        source_acceptance = $source.acceptance
        source_ask_exit_code = $source.ask_exit_code
        model = $source.model
        runtime = $source.runtime_version.stderr
        endpoint = $source.endpoint
        model_calls = $source.model_calls
        finish_reason = 'stop (as recorded in the preserved run)'
        tool_trace = $trace
        citation_status = $offline.quality.components.citation
        numeric_status = $offline.quality.components.numeric
        overall_quality = $offline.quality.status
        fixture_unchanged = $source.fixture_unchanged
        cleanup = $source.cleanup
        model_service_currently_listening = $serviceState.listener_present
        evidence = @(
            'reports/m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/m2-real-model-run.json',
            'reports/m2_real_model_runs/20261023T-m2-qwen35-4b-r6-ctx4096/offline-quality-reassessment-static-numeric.json'
        )
        note = 'This option replays preserved evidence and never starts a model or sends a new request. M2 remains PARTIAL because numeric quality is UNVERIFIABLE.'
    }
    Write-JsonFile (Join-Path $RunDir 'm2-demo-summary.json') $summary
    $summary | ConvertTo-Json -Depth 12
    if ($summary.overall_quality -eq 'PASS' -and $summary.source_status -eq 'PASS') {
        exit 0
    }
    exit 3
}

if ($Demo -eq 'm3') {
    $logPath = Join-Path $RunDir 'm3-demo.log'
    $taskId = "{0}-task" -f $RunId
    & $Python $M3Script --approve --task-id $taskId --run-id $RunId *> $logPath
    $demoExit = $LASTEXITCODE
    $summary = [ordered]@{
        demo_run_id = $RunId
        task_id = $taskId
        run_id = $RunId
        kind = 'synthetic_m3_repair_control_flow'
        command = ('.venv\\Scripts\\python.exe scripts\\demo_m3_repair.py --approve --task-id {0} --run-id {1}' -f $taskId, $RunId)
        exit_code = $demoExit
        log = (Join-Path 'reports/interview_demo_runs' (Join-Path $RunId 'm3-demo.log'))
        model_started = $false
        source_workspace_written = $false
        expected_flow = @('baseline pytest failed', 'diagnostic generated', 'diff proposed', 'independent synthetic approval', 'SQLite ledger and one-time receipt', 'isolated copy patched', 'post-apply pytest passed', 'source hash unchanged')
        note = 'The patch and reviewer are deterministic synthetic controls; this does not prove model-generated repair or an OS-level sandbox.'
    }
    Write-JsonFile (Join-Path $RunDir 'm3-demo-summary.json') $summary
    $summary | ConvertTo-Json -Depth 10
    if ($demoExit -ne 0) {
        exit 3
    }
    exit 0
}

Fail-Stage 'UNKNOWN_DEMO' $Demo
