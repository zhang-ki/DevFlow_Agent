param(
    [string]$RepoRoot = (Get-Location).Path,
    [string]$RunId = "20261023T-m2-qwen35-4b",
    [string]$ModelRoot = $env:DEVFLOW_MODEL_ROOT
)

$ErrorActionPreference = 'Stop'
if ($RunId -notmatch '^[A-Za-z0-9-]+$') { throw 'invalid_run_id' }
if ([string]::IsNullOrWhiteSpace($ModelRoot)) { throw 'DEVFLOW_MODEL_ROOT_not_set' }
$ModelRoot = (Resolve-Path -LiteralPath $ModelRoot -ErrorAction Stop).Path
$runRoot = Join-Path $RepoRoot ("reports\m2_real_model_runs\" + $RunId)
if (Test-Path -LiteralPath $runRoot) { throw 'run_directory_already_exists' }
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$helper = Join-Path $RepoRoot 'scripts\m1_5_http_support.py'
$server = Join-Path $ModelRoot 'qwen3.5-4b\.runtime\llama-b11424\llama-server.exe'
$model = Join-Path $ModelRoot 'qwen3.5-4b\Qwen3.5-4B-Q4_K_M.gguf'
$env:DEVFLOW_LLAMA_SERVER = $server
$port = 11434
$alias = 'devflow-qwen35-4b'
$contextTokens = 4096
$maxOutputTokens = 1536
$expectedModelSha256 = '00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4'
$expectedRuntimeSha256 = 'f8df891a5e4d10fa8cfc0f86c8d99617a06edd18d046e18b59ec3354f17fc12d'
$fixture = Join-Path $runRoot 'synthetic_repo'
$baselinePytestOut = Join-Path $runRoot 'baseline-pytest.log'
$serverOut = Join-Path $runRoot 'llama-server.stdout.log'
$serverErr = Join-Path $runRoot 'llama-server.stderr.log'
$askOut = Join-Path $runRoot 'ask.stdout.json'
$askErr = Join-Path $runRoot 'ask.stderr.log'
$serverProc = $null
$askProc = $null

function Save-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
}
function Tree-Hashes([string]$Root) {
    $items = @()
    if (Test-Path -LiteralPath $Root) {
        foreach ($f in Get-ChildItem -LiteralPath $Root -File -Recurse | Sort-Object FullName) {
            $rel = $f.FullName.Substring($Root.Length).TrimStart('\').Replace('\', '/')
            $items += [ordered]@{ path = $rel; sha256 = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); bytes = $f.Length }
        }
    }
    return ,$items
}
function Snapshot() {
    @(Get-CimInstance Win32_Process | ForEach-Object {
        [pscustomobject]@{ pid = [int]$_.ProcessId; parent_pid = [int]$_.ParentProcessId; path = $_.ExecutablePath; created = [string]$_.CreationDate }
    })
}
function Descendants($Snapshot, [int[]]$Roots) {
    $set = [Collections.Generic.HashSet[int]]::new()
    foreach ($r in $Roots) { if ($r -gt 0) { [void]$set.Add($r) } }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($p in $Snapshot) {
            if ($set.Contains($p.parent_pid) -and -not $set.Contains($p.pid)) { [void]$set.Add($p.pid); $changed = $true }
        }
    }
    return @($set)
}

$run = [ordered]@{
    run_id = $RunId
    status = 'NOT_RUN'
    model = 'Qwen3.5-4B Q4_K_M'
    model_path = $model
    runtime_path = $server
    endpoint = 'http://127.0.0.1:11434/v1'
    model_sha256 = $null
    runtime_sha256 = $null
    runtime_version = $null
    runtime_help = $null
    context_tokens = $contextTokens
    max_output_tokens = $maxOutputTokens
    server_args = $null
    max_steps = 8
    timeout_seconds = 180
    question = 'Diagnose the captured pytest failure in failure.txt. Perform one repository search for total, then read the failure report, test, and implementation at most once each. Return exactly these headings: Summary; Root cause; Observed result; Evidence; Confidence; Unconfirmed. Under Root cause include `Diagnosis class: VERIFIED_ASSERTION_MISMATCH` only when the observed result, test expectation, and safe static source result agree; otherwise use UNVERIFIABLE. Under Observed result write `Observed result: actual=<number>; expected=<number>`. Do not treat a test expectation as an observed result or claim intended behavior beyond the files. Cite the log, test, and implementation with exact relative paths, inclusive line ranges, and file SHA-256.'
    tool_trace = @()
    answer = $null
    ask_exit_code = $null
    ask_elapsed_seconds = $null
    fixture_hashes_before = @()
    fixture_hashes_after = @()
    fixture_unchanged = $null
    baseline_pytest = $null
    cleanup = $null
    error_type = $null
    error_message = $null
}

try {
    if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $helper) -or !(Test-Path -LiteralPath $server) -or !(Test-Path -LiteralPath $model)) { throw 'required_local_model_files_missing' }
    $run.model_sha256 = (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant()
    $run.runtime_sha256 = (Get-FileHash -LiteralPath $server -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($run.model_sha256 -ne $expectedModelSha256) { throw 'model_sha256_does_not_match_verified_qwen_candidate' }
    if ($run.runtime_sha256 -ne $expectedRuntimeSha256) { throw 'runtime_sha256_does_not_match_verified_llama_cpp_candidate' }
    $version = & $python $helper version --run-dir $runRoot
    if ($LASTEXITCODE -ne 0) { throw 'runtime_version_preflight_failed' }
    $run.runtime_version = $version | ConvertFrom-Json
    $profileOutput = @(& $python $helper profile --run-dir $runRoot --project-root $RepoRoot)
    $profileExit = $LASTEXITCODE
    $profileText = $profileOutput -join "`n"
    try {
        $run.model_profile_preflight = $profileText | ConvertFrom-Json
    }
    catch {
        $run.model_profile_preflight = [ordered]@{
            stage = 'model_config_preflight'
            status = 'FAIL'
            failure_stage = 'decode_preflight_result'
            exception_type = $_.Exception.GetType().Name
            error_code = 'preflight_output_invalid'
            diagnostic = 'The model configuration preflight returned an invalid diagnostic record'
        }
    }
    if ($profileExit -ne 0 -or $run.model_profile_preflight.status -ne 'PASS') { throw 'model_config_preflight_failed' }
    $profile = $run.model_profile_preflight
    if ($profile.model -ne $alias -or $profile.endpoint -ne "http://127.0.0.1:$port/v1") {
        throw 'model_config_identity_or_loopback_endpoint_mismatch'
    }
    $helpOutput = @(& $server '--help' 2>&1)
    $helpExit = $LASTEXITCODE
    $helpLines = @($helpOutput | ForEach-Object { [string]$_ })
    [IO.File]::WriteAllLines((Join-Path $runRoot 'runtime-help.log'), $helpLines, [Text.UTF8Encoding]::new($false))
    $run.runtime_help = [ordered]@{
        argv = @($server, '--help')
        exit_code = $helpExit
        supports_ctx_size = (($helpOutput -join "`n") -match '(?m)--ctx-size(?:[ =]|$)')
    }
    if ($helpExit -ne 0 -or -not $run.runtime_help.supports_ctx_size) { throw 'runtime_context_parameter_unsupported' }
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) { throw 'model_port_already_owned' }
    $freeRamGb = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory * 1KB / 1GB, 2)
    $modelGb = [math]::Round((Get-Item -LiteralPath $model).Length / 1GB, 2)
    if ($freeRamGb -lt ($modelGb + 1.5)) { throw 'resource_preflight_insufficient_free_memory' }

    $pytest = Get-Command pytest -ErrorAction SilentlyContinue
    if (-not $pytest) { throw 'pytest_executable_not_available' }
    New-Item -ItemType Directory -Path (Join-Path $fixture 'src') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $fixture 'tests') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixture 'src\totals.py'), "def total(values):`n    return sum(values) + 1`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $fixture 'tests\test_totals.py'), "from src.totals import total`n`ndef test_total():`n    assert total([1, 2]) == sum([1, 2])`n", [Text.UTF8Encoding]::new($false))
    $previousBytecode = $env:PYTHONDONTWRITEBYTECODE
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = $fixture
    Push-Location $fixture
    try {
        $pytestLines = @(& $pytest.Source '-q' '--color=no' '-p' 'no:cacheprovider' 'tests/test_totals.py' 2>&1 | ForEach-Object { [string]$_ })
        $pytestExit = $LASTEXITCODE
    } finally {
        Pop-Location
        if ($null -eq $previousBytecode) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue }
        else { $env:PYTHONDONTWRITEBYTECODE = $previousBytecode }
        if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
        else { $env:PYTHONPATH = $previousPythonPath }
    }
    [IO.File]::WriteAllLines($baselinePytestOut, $pytestLines, [Text.UTF8Encoding]::new($false))
    $pytestText = $pytestLines -join "`n"
    $baselineOutput = @(& $python $helper pytest-baseline --run-dir $runRoot --log-file $baselinePytestOut --pytest-exit-code $pytestExit --expected-nodeid 'tests/test_totals.py::test_total' --expected-actual 4 --expected-value 3)
    $baselineValidationExit = $LASTEXITCODE
    try {
        $baselineValidation = ($baselineOutput -join "`n") | ConvertFrom-Json
    }
    catch {
        $baselineValidation = [ordered]@{
            stage = 'synthetic_pytest_baseline_check'
            status = 'FAIL'
            error_code = 'baseline_result_invalid'
        }
    }
    $run.baseline_pytest = [ordered]@{
        executable = $pytest.Source
        argv = @('-q', '--color=no', '-p', 'no:cacheprovider', 'tests/test_totals.py')
        expected_exit_code = 1
        exit_code = $pytestExit
        expected_failure_confirmed = ($pytestExit -eq 1)
        captured_output_path = 'baseline-pytest.log'
        contains_assertion_failure = ($baselineValidation.assertion_match -eq $true)
        validation = $baselineValidation
    }
    if ($baselineValidationExit -ne 0 -or $baselineValidation.status -ne 'PASS') { throw 'synthetic_pytest_baseline_did_not_match_expected_failure' }
    [IO.File]::WriteAllText((Join-Path $fixture 'failure.txt'), $pytestText + "`n", [Text.UTF8Encoding]::new($false))
    $run.fixture_hashes_before = Tree-Hashes $fixture

    $args = '-m "' + $model + '" --host 127.0.0.1 --port 11434 --alias ' + $alias + ' --ctx-size ' + $contextTokens + ' --threads 8 --batch-size 256 --ubatch-size 64 --n-gpu-layers 0 --parallel 1 --jinja'
    $run.server_args = @('-m', $model, '--host', '127.0.0.1', '--port', [string]$port, '--alias', $alias, '--ctx-size', [string]$contextTokens, '--threads', '8', '--batch-size', '256', '--ubatch-size', '64', '--n-gpu-layers', '0', '--parallel', '1', '--jinja')
    $serverProc = Start-Process -FilePath $server -ArgumentList $args -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr
    $run.server_pid = $serverProc.Id
    $healthText = & $python $helper health --run-dir $runRoot
    if ($LASTEXITCODE -ne 0) { throw 'health_or_models_gate_failed' }
    $health = $healthText | ConvertFrom-Json
    $run.health = $health

    $safeQuestion = $run.question.Replace('"', '\"')
    $askArgs = '-u "' + $helper + '" ask --run-dir "' + $runRoot + '" --repo "' + $fixture + '" --question "' + $safeQuestion + '" --max-steps 8 --timeout-seconds 180 --max-output-tokens ' + $maxOutputTokens
    $started = [Diagnostics.Stopwatch]::StartNew()
    $askProc = Start-Process -FilePath $python -ArgumentList $askArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $askOut -RedirectStandardError $askErr
    while (-not $askProc.HasExited -and $started.Elapsed.TotalSeconds -lt 190) { Start-Sleep -Milliseconds 250; $askProc.Refresh() }
    if (-not $askProc.HasExited) { Stop-Process -Id $askProc.Id -Force -ErrorAction SilentlyContinue; $askProc.WaitForExit(10000) | Out-Null; $run.status = 'TIMEOUT' }
    else { $askProc.WaitForExit(); $askProc.Refresh(); $run.ask_exit_code = $askProc.ExitCode; $run.status = if ($askProc.ExitCode -eq 0) { 'PASS' } else { 'FAIL' } }
    $started.Stop(); $run.ask_elapsed_seconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
    if (Test-Path -LiteralPath $askOut) {
        try {
            $ask = Get-Content -LiteralPath $askOut -Raw | ConvertFrom-Json
            $run.ask_result_status = $ask.status
            $run.answer = $ask.answer
            $run.tool_trace = $ask.tool_trace
            $run.citations = $ask.citations
            $run.model_calls = $ask.model_calls
            $run.answer_quality = $ask.answer_quality
            $run.acceptance = if ($ask.status -eq 'verified' -and $ask.answer_quality.status -eq 'PASS' -and $ask.answer_quality.diagnostic_check.status -eq 'PASS' -and $ask.citations.valid -and @($ask.tool_trace).Count -ge 4) { 'REAL_MODEL_DIAGNOSTIC_READONLY_PASS' } else { 'REAL_MODEL_DIAGNOSTIC_NOT_VERIFIED' }
        } catch { $run.error_type = 'ask_result_parse_error'; $run.error_message = $_.Exception.Message }
    }
} catch {
    $run.status = 'BLOCKED'
    $run.error_type = $_.Exception.GetBaseException().GetType().Name
    $run.error_message = $_.Exception.GetBaseException().Message
} finally {
    $snapshot = Snapshot
    $roots = @(); if ($serverProc) { $roots += [int]$serverProc.Id }; if ($askProc) { $roots += [int]$askProc.Id }
    $owned = @(Descendants $snapshot $roots)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    foreach ($childPid in ($owned | Sort-Object -Descending)) {
        if ($childPid -eq $PID) { continue }
        $p = Get-Process -Id $childPid -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $childPid -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 500
    $sw.Stop()
    $remaining = @(Descendants (Snapshot) $roots | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    $run.cleanup = [ordered]@{ owned_pids = $owned; remaining_pids = $remaining; shutdown_elapsed_seconds = [math]::Round($sw.Elapsed.TotalSeconds, 3); listener_after_cleanup = @((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)).Count -gt 0 }
    $run.fixture_hashes_after = Tree-Hashes $fixture
    $run.fixture_unchanged = (($run.fixture_hashes_before | ConvertTo-Json -Compress) -eq ($run.fixture_hashes_after | ConvertTo-Json -Compress))
    if (@($remaining).Count -ne 0 -or $run.cleanup.listener_after_cleanup) { $run.acceptance = 'REAL_MODEL_DIAGNOSTIC_NOT_VERIFIED' }
    Save-Json (Join-Path $runRoot 'm2-real-model-run.json') $run
    Write-Output ('RUN_ID=' + $RunId)
    Write-Output ('STATUS=' + $run.status + ' ACCEPTANCE=' + $run.acceptance)
    Write-Output ('ASK_EXIT=' + $run.ask_exit_code + ' MODEL_CALLS=' + $run.model_calls)
    Write-Output ('FIXTURE_UNCHANGED=' + $run.fixture_unchanged + ' REMAINING=' + @($remaining).Count)
}
if ($run.acceptance -ne 'REAL_MODEL_DIAGNOSTIC_READONLY_PASS') { exit 3 }
