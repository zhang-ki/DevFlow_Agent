param([string]$RepoRoot,[string]$RunId,[string]$ModelRoot=$env:DEVFLOW_MODEL_ROOT)
$ErrorActionPreference='Stop'
if($RunId -notmatch '^[a-zA-Z0-9-]+$'){throw 'invalid_run_id'}
if([string]::IsNullOrWhiteSpace($ModelRoot)){throw 'DEVFLOW_MODEL_ROOT_not_set'}
$ModelRoot=(Resolve-Path -LiteralPath $ModelRoot -ErrorAction Stop).Path
$runRoot=Join-Path $RepoRoot ('reports\m1_5_real_model_runs\'+$RunId)
if(Test-Path -LiteralPath $runRoot){throw 'run_directory_already_exists'}
New-Item -ItemType Directory -Path $runRoot | Out-Null
$helper=Join-Path $RepoRoot 'scripts\m1_5_http_support.py'
$serverPath=Join-Path $ModelRoot 'qwen3.5-4b\.runtime\llama-b11424\llama-server.exe'
$modelPath=Join-Path $ModelRoot 'qwen3.5-4b\Qwen3.5-4B-Q4_K_M.gguf'
$env:DEVFLOW_LLAMA_SERVER=$serverPath
$python=Join-Path $RepoRoot '.venv\Scripts\python.exe'
$port=11434
$alias='devflow-qwen35-4b'
$fixture=Join-Path $runRoot 'synthetic_repo'
$stdoutServer=Join-Path $runRoot 'llama-server.stdout.log'
$stderrServer=Join-Path $runRoot 'llama-server.stderr.log'
$askOut=Join-Path $runRoot 'ask.stdout.json'
$askErr=Join-Path $runRoot 'ask.stderr.log'
$serverProc=$null
$askProc=$null
$http=$null
$healthAttempts=[System.Collections.Generic.List[object]]::new()
$run=[ordered]@{
 run_id=$RunId;status='NOT_RUN';model='Qwen3.5-4B Q4_K_M';model_alias=$alias
 model_path=$modelPath;model_sha256=$null;runtime_path=$serverPath;runtime_sha256=$null
 runtime_version=$null
 endpoint='http://127.0.0.1:11434/v1';context_tokens=3072;max_output_tokens=1024
 native_tool_calls='llama.cpp b11424 --jinja; OpenManus OpenAI-compatible tool calls'
 start_command=$null;server_pid=$null;server_started_at_utc=$null
 health_status='NOT_RUN';health_http_status=$null;health_body=$null;health_elapsed_seconds=$null
 models_status='NOT_RUN';models_http_status=$null;models_match_alias=$false
 ask_status='NOT_RUN';ask_exit_code=$null;ask_timed_out=$false
 ask_started_at_utc=$null;ask_elapsed_seconds=$null;ask_result_status=$null
 answer_quality_status=$null;numeric_claims_checked=$null;numeric_claims=@()
 citation_valid=$null;correction_attempts=$null;model_calls=$null;finish_reason=$null
 allowed_tools=@();tool_trace=@();error_type=$null;error_message=$null
 fixture_hashes_before=@();fixture_hashes_after=@();fixture_unchanged=$null
 evaluation_checks=[ordered]@{search_then_read=$false;only_allowlisted_tools=$false;citation_matches_read=$false;explicit_arithmetic_verified=$false}
}
$cleanup=[ordered]@{server_pid=$null;ask_pid=$null;owned_processes_before_cleanup=@();server_terminated_by_this_run=$false;server_exit_code=$null;ask_terminated_by_this_run=$false;ask_exit_code=$null;shutdown_elapsed_seconds=$null;remaining_owned_processes=@();listener_after_cleanup=$null;residual_server=$false}
function Save-Json([string]$Path,$Value){[IO.File]::WriteAllText($Path,($Value|ConvertTo-Json -Depth 14),[Text.UTF8Encoding]::new($false))}
function Get-TreeHashes([string]$Root){$items=@();if(Test-Path $Root){foreach($f in Get-ChildItem -LiteralPath $Root -File -Recurse|Sort-Object FullName){$rel=$f.FullName.Substring($Root.Length).TrimStart('\').Replace('\','/');$items+=,[ordered]@{path=$rel;sha256=(Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLowerInvariant();bytes=$f.Length}}};return ,$items}
function Get-Snapshot{return @(Get-CimInstance Win32_Process|ForEach-Object{[pscustomobject]@{pid=[int]$_.ProcessId;parent_pid=[int]$_.ParentProcessId;path=$_.ExecutablePath;created=[string]$_.CreationDate}})}
function Get-OwnedPids($Snapshot,[int[]]$Roots){$set=[System.Collections.Generic.HashSet[int]]::new();foreach($r in $Roots){if($r -gt 0){[void]$set.Add($r)}};$changed=$true;while($changed){$changed=$false;foreach($p in $Snapshot){if($set.Contains($p.parent_pid)-and -not $set.Contains($p.pid)){[void]$set.Add($p.pid);$changed=$true}}};return @($set)}
try {
 $run.stage='preflight'
 $run.initial_processes=@(Get-Snapshot | Where-Object {$_.path -and $_.path -match 'llama-server.exe$'})
 $run.python_path=$python;$run.python_version=(& $python --version)
 $run.script_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash
 $run.helper_sha256=(Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash
 if(!(Test-Path $serverPath)-or !(Test-Path $modelPath)-or !(Test-Path $python)){throw 'preflight_required_file_missing'}
 $profileText=& $python -c "import json; from pathlib import Path; from devflow_agent.real_readonly_agent import load_local_model_profile; p=load_local_model_profile(Path.cwd()); print(json.dumps({'model':p.model,'endpoint':p.endpoint_sanitized,'max_tokens':p.max_tokens}))"
 if($LASTEXITCODE -ne 0){throw 'local_model_profile_load_failed'}
 $profile=$profileText|ConvertFrom-Json
 if($profile.model -ne $alias-or $profile.endpoint -ne 'http://127.0.0.1:11434/v1'){throw 'configured_model_or_endpoint_mismatch'}
 $run.model_sha256=(Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant()
 $run.runtime_sha256=(Get-FileHash -LiteralPath $serverPath -Algorithm SHA256).Hash.ToLowerInvariant()
 if($run.model_sha256 -ne '00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4'){throw 'model_hash_mismatch'}
 if($run.runtime_sha256 -ne 'f8df891a5e4d10fa8cfc0f86c8d99617a06edd18d046e18b59ec3354f17fc12d'){throw 'runtime_hash_mismatch'}
 $versionText=& $python $helper version --run-dir $runRoot
 $versionExit=$LASTEXITCODE
 if($versionExit -ne 0){throw 'actual_runtime_version_preflight_failed'}
 $version=$versionText|ConvertFrom-Json;$run.runtime_version=$version
 $run.config_sha256=(Get-FileHash -LiteralPath (Join-Path $RepoRoot 'config\config.toml') -Algorithm SHA256).Hash
 git -C $RepoRoot check-ignore --quiet config/config.toml
 if($LASTEXITCODE -ne 0){throw 'private_config_not_ignored'}
 $os=Get-CimInstance Win32_OperatingSystem;$freeRamGb=[math]::Round($os.FreePhysicalMemory*1KB/1GB,2);$modelGb=(Get-Item -LiteralPath $modelPath).Length/1GB
 $run.resource_preflight=[ordered]@{free_ram_gb=$freeRamGb;model_file_gb=[math]::Round($modelGb,2);minimum_threshold_gb=[math]::Round($modelGb+1.5,2);passed=($freeRamGb -ge ($modelGb+1.5))}
 if(!$run.resource_preflight.passed){throw 'resource_preflight_insufficient_free_memory'}
 $listeners=@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
 if($listeners.Count -gt 0){throw 'model_port_already_owned'}
 $tcp=[System.Net.Sockets.TcpClient]::new()
 try{$p=$tcp.BeginConnect('127.0.0.1',$port,$null,$null);$reachable=$p.AsyncWaitHandle.WaitOne(1000);if($reachable){try{$tcp.EndConnect($p);if($tcp.Connected){throw 'model_port_already_in_use'}}catch{if($_.Exception.Message -eq 'model_port_already_in_use'){throw}}}}finally{$tcp.Dispose()}
 New-Item -ItemType Directory -Path (Join-Path $fixture 'src') -Force|Out-Null
 $nl=[Environment]::NewLine
 [IO.File]::WriteAllText((Join-Path $fixture 'README.md'),('Synthetic M1.5 repository. Signed totals retain negative values.'+$nl),[Text.UTF8Encoding]::new($false))
 $source=@('def total(values: list[int]) -> int:','    """Return the signed arithmetic total of all values."""','    return sum(values)')
 [IO.File]::WriteAllText((Join-Path $fixture 'src\totals.py'),(($source -join $nl)+$nl),[Text.UTF8Encoding]::new($false))
 $run.fixture_hashes_before=Get-TreeHashes $fixture
 $fixtureCheck=& $python -m devflow_agent.cli read --repo $fixture --path src/totals.py --start-line 1 --end-line 3
 if($LASTEXITCODE -ne 0){throw 'synthetic_repository_read_preflight_failed'}
 Save-Json (Join-Path $runRoot 'fixture-read-preflight.json') ($fixtureCheck|ConvertFrom-Json)
 $run.stage='server_start'

 $startArgs='-m "'+$modelPath+'" --host 127.0.0.1 --port 11434 --alias '+$alias+' --ctx-size 3072 --threads 8 --batch-size 256 --ubatch-size 64 --n-gpu-layers 0 --parallel 1 --jinja'
 $run.start_command='"'+$serverPath+'" '+$startArgs
 $run.server_started_at_utc=(Get-Date).ToUniversalTime().ToString('o')
 $serverProc=Start-Process -FilePath $serverPath -ArgumentList $startArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutServer -RedirectStandardError $stderrServer
 $run.server_pid=$serverProc.Id;$cleanup.server_pid=$serverProc.Id
 $run.stage='health'
 $healthText=& $python $helper health --run-dir $runRoot
 $healthExit=$LASTEXITCODE
 $health=$healthText|ConvertFrom-Json
 $run.health_status=$health.health;$run.health_elapsed_seconds=$health.health_elapsed_seconds
 foreach($item in $health.attempts){$healthAttempts.Add($item)}
 $run.health_http_status=$health.attempts[-1].http_status;$run.health_body=$health.attempts[-1].body
 $run.models_status=$health.models;$run.models_http_status=$health.models_response.http_status
 $run.models_match_alias=$health.models_match_alias
 if($healthExit -ne 0){throw 'python_health_or_models_gate_failed'}
 $run.stage='listener_ownership'
 $ownedListening=@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | Select-Object LocalAddress,LocalPort,OwningProcess)
 $run.actual_listeners=$ownedListening
 if($ownedListening.Count -ne 1 -or $ownedListening[0].LocalAddress -ne '127.0.0.1' -or $ownedListening[0].OwningProcess -ne $serverProc.Id){throw 'listener_ownership_or_bind_mismatch'}
 $question='Find the helper that totals signed amounts in this repository. Explain how positive and negative values are combined. Compute sum([-100, 50, -200]) and state the expression and result explicitly with calculation steps. Cite the exact relative source path and inclusive line range you actually read, in path lines start-end form.'
 $run.stage='ask'
 $run.status='RUNNING_ASK';$run.ask_started_at_utc=(Get-Date).ToUniversalTime().ToString('o')
 $run.question=$question
 $askArgs='-u "'+$helper+'" ask --run-dir "'+$runRoot+'" --repo "'+$fixture+'" --question "'+$question+'" --max-steps 6 --timeout-seconds 180'
 $askProc=Start-Process -FilePath $python -ArgumentList $askArgs -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $askOut -RedirectStandardError $askErr;$cleanup.ask_pid=$askProc.Id
 $askTimer=[System.Diagnostics.Stopwatch]::StartNew();while($askTimer.Elapsed.TotalSeconds -lt 190 -and -not $askProc.HasExited){Start-Sleep -Milliseconds 250;$askProc.Refresh()};$askTimer.Stop()
 $run.ask_timed_out=-not $askProc.HasExited
 if($run.ask_timed_out){Stop-Process -Id $askProc.Id -Force -ErrorAction SilentlyContinue;$cleanup.ask_terminated_by_this_run=$true;$askProc.WaitForExit(10000)|Out-Null}else{$askProc.WaitForExit();$askProc.Refresh();$run.ask_exit_code=$askProc.ExitCode;$cleanup.ask_exit_code=$askProc.ExitCode}
 $run.ask_elapsed_seconds=[math]::Round($askTimer.Elapsed.TotalSeconds,3);$run.ask_status=if($run.ask_timed_out){'TIMEOUT'}elseif($run.ask_exit_code -eq 0){'PASS'}else{'FAIL'};$run.status=$run.ask_status
 if(Test-Path $askOut){try{$a=Get-Content -LiteralPath $askOut -Raw|ConvertFrom-Json;$run.ask_result_status=$a.status;$run.answer_quality_status=$a.answer_quality.status;$run.numeric_claims_checked=$a.answer_quality.numeric_check.claims_checked;$run.numeric_claims=$a.answer_quality.numeric_check.claims;$run.numeric_status=$a.answer_quality.numeric_check.status;$run.final_answer=$a.answer;$run.citation_check=$a.citations;$run.trace_issues=$a.tool_trace_issues;$run.quality_correction=$a.quality_correction;$run.citation_valid=$a.citations.valid;$run.correction_attempts=$a.quality_correction.attempts;$run.model_calls=$a.model_calls;$run.finish_reason=$a.finish_reason;$run.allowed_tools=$a.allowed_tools;$run.tool_trace=$a.tool_trace
 $tools=@($a.tool_trace|ForEach-Object{$_.tool});$run.evaluation_checks.search_then_read=($tools.Count -ge 2 -and $tools[0] -eq 'repository_search_readonly' -and $tools[1] -eq 'repository_readonly_read');$run.evaluation_checks.only_allowlisted_tools=(@($tools|Where-Object{$_ -notin @('repository_search_readonly','repository_readonly_read')}).Count -eq 0);$run.evaluation_checks.citation_matches_read=[bool]$a.citations.valid;$run.evaluation_checks.explicit_arithmetic_verified=($a.answer_quality.numeric_check.claims_checked -gt 0 -and @($a.answer_quality.numeric_check.claims|Where-Object{$_.verification -eq 'PASS' -and ($_.expression -replace '\s','') -eq 'sum([-100,50,-200])' -and $_.actual_value -eq -250 -and $_.claimed_value -eq -250}).Count -gt 0)
 }catch{$run.error_type='AskResultParseError';$run.error_message=$_.Exception.GetType().Name}}
 if($run.ask_status -eq 'PASS' -and $run.ask_result_status -eq 'verified' -and $run.answer_quality_status -eq 'PASS' -and $run.fixture_unchanged -ne $false -and $run.evaluation_checks.search_then_read -and $run.evaluation_checks.only_allowlisted_tools -and $run.evaluation_checks.citation_matches_read -and $run.evaluation_checks.explicit_arithmetic_verified){$run.acceptance='REAL_MODEL_READONLY_ANSWER_GATE_PASS'}else{$run.acceptance='REAL_MODEL_READONLY_ANSWER_GATE_NOT_VERIFIED'}
} catch {if($run.status -eq 'NOT_RUN'){$run.status='BLOCKED'};$run.error_type=$_.Exception.GetBaseException().GetType().Name;$run.error_message=$_.Exception.GetBaseException().Message
} finally {
 if($http){$http.Dispose()}
 $snapshot=Get-Snapshot;$roots=@();if($serverProc){$roots+=,[int]$serverProc.Id};if($askProc){$roots+=,[int]$askProc.Id};$ownedPids=Get-OwnedPids $snapshot $roots;$owned=@($snapshot|Where-Object{$ownedPids -contains $_.pid});$cleanup.owned_processes_before_cleanup=$owned
 $sw=[System.Diagnostics.Stopwatch]::StartNew()
 if($askProc){$askProc.Refresh();if(-not $askProc.HasExited){Stop-Process -Id $askProc.Id -Force -ErrorAction SilentlyContinue;$cleanup.ask_terminated_by_this_run=$true;$askProc.WaitForExit(10000)|Out-Null};if($askProc.HasExited){$askProc.Refresh();$cleanup.ask_exit_code=$askProc.ExitCode}}
 if($serverProc){$serverProc.Refresh();if(-not $serverProc.HasExited){Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue;$cleanup.server_terminated_by_this_run=$true;$serverProc.WaitForExit(10000)|Out-Null};if($serverProc.HasExited){$serverProc.Refresh();$cleanup.server_exit_code=$serverProc.ExitCode}}
 $roots=@();if($serverProc){$roots+=,[int]$serverProc.Id};if($askProc){$roots+=,[int]$askProc.Id}
 foreach($child in ($owned|Sort-Object { $_.pid } -Descending)){if($roots -contains $child.pid){continue};$current=Get-CimInstance Win32_Process -Filter "ProcessId = $($child.pid)" -ErrorAction SilentlyContinue;if($current -and [string]$current.CreationDate -eq $child.created){Stop-Process -Id $child.pid -Force -ErrorAction SilentlyContinue}}
 $sw.Stop();$cleanup.shutdown_elapsed_seconds=[math]::Round($sw.Elapsed.TotalSeconds,3);Start-Sleep -Milliseconds 300;$post=Get-Snapshot;$cleanup.remaining_owned_processes=@($post|Where-Object{$ownedPids -contains $_.pid})
 $tcp=[System.Net.Sockets.TcpClient]::new();try{$p=$tcp.BeginConnect('127.0.0.1',$port,$null,$null);$reachable=$p.AsyncWaitHandle.WaitOne(1000);if($reachable){try{$tcp.EndConnect($p)}catch{$reachable=$false}};$cleanup.listener_after_cleanup=[bool]$tcp.Connected}catch{$cleanup.listener_after_cleanup=$false}finally{$tcp.Dispose()}
 $cleanup.residual_server=[bool](@(Get-CimInstance Win32_Process -Filter "name = 'llama-server.exe'"|Where-Object{$_.ProcessId -eq $run.server_pid}).Count -gt 0)
 $run.fixture_hashes_after=Get-TreeHashes $fixture
 if($run.fixture_hashes_before.Count -gt 0){$run.fixture_unchanged=(($run.fixture_hashes_before|ConvertTo-Json -Compress)-eq($run.fixture_hashes_after|ConvertTo-Json -Compress))}
 if($run.fixture_unchanged -ne $true -or @($cleanup.remaining_owned_processes).Count -ne 0 -or $cleanup.listener_after_cleanup){$run.acceptance='REAL_MODEL_READONLY_ANSWER_GATE_NOT_VERIFIED'}
 if($run.status -eq 'RUNNING_ASK'){$run.status='FAIL'}
 Save-Json (Join-Path $runRoot 'ask-run.json') $run;Save-Json (Join-Path $runRoot 'server-health.json') ([ordered]@{attempts=$healthAttempts;health=$run.health_status;models=$run.models_status;models_match_alias=$run.models_match_alias});Save-Json (Join-Path $runRoot 'process-cleanup.json') $cleanup
 Write-Output ('RUN_ID='+$RunId);Write-Output ('STATUS='+$run.status+' ACCEPTANCE='+$run.acceptance);Write-Output ('HEALTH='+$run.health_status+' MODELS='+$run.models_status+' ASK='+$run.ask_status);Write-Output ('MODEL_CALLS='+$run.model_calls+' QUALITY='+$run.answer_quality_status+' CORRECTIONS='+$run.correction_attempts);Write-Output ('FIXTURE_UNCHANGED='+$run.fixture_unchanged);Write-Output ('CLEANUP_SECONDS='+$cleanup.shutdown_elapsed_seconds+' REMAINING='+@($cleanup.remaining_owned_processes).Count+' LISTENER_AFTER='+$cleanup.listener_after_cleanup)
}
if($run.acceptance -ne 'REAL_MODEL_READONLY_ANSWER_GATE_PASS'){exit 3}
