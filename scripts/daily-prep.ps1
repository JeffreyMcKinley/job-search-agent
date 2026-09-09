# Daily `main.py prep` runner: prepares materials for everything in "to apply".
#
# Unlike daily-job-search.ps1 this does NOT wrap `claude -p` itself --
# main.py prep spawns its own agent subprocess (utils/prep_runner.py) and
# owns the budgets, retries and sheet writes. Wrapping it again would nest
# two agents and break the result-envelope contract.
#
# No browser required: prep resolves postings via WebFetch and the ATS APIs.

$ErrorActionPreference = 'Stop'
$proj    = 'D:\Code\HelperAgents'
$py      = Join-Path $proj 'venv\Scripts\python.exe'
$main    = Join-Path $proj 'job_tracker_agent\main.py'
$logDir  = Join-Path $proj 'logs'
$stamp   = Get-Date -Format 'yyyy-MM-dd'
$log     = Join-Path $logDir "prep-$stamp.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

foreach ($p in @($py, $main)) {
    if (-not (Test-Path $p)) {
        "=== $(Get-Date -Format 'u') ABORT: missing $p ===" |
            Out-File -FilePath $log -Append -Encoding utf8
        exit 1
    }
}

Set-Location $proj

"=== run $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8

# prep enforces its own total budget: min(300 + 420*jobs, 4h), plus up to
# 660s of transient-rejection backoff. The task's ExecutionTimeLimit is only
# a backstop above that.
& $py $main prep 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
$rc = $LASTEXITCODE

"=== exit $rc ===" | Out-File -FilePath $log -Append -Encoding utf8

# Board state after the run, so the log is self-contained.
& $py $main status 2>&1 | Out-File -FilePath $log -Append -Encoding utf8

Get-ChildItem $logDir -Filter 'prep-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force

exit $rc
