# Daily /job-tracker search runner.
#
# The skill's `search` step now covers two sources: job-alert email (scan-leads)
# and a fresh job search. Both run under the single /job-tracker search
# invocation below, so nothing extra is dispatched from here.
#
# Requires: Brave running with the Claude extension connected, and
# hiring.cafe permitted in that extension. The extension lives in Brave on
# this machine, not Chrome -- launching chrome.exe gets a browser the
# extension cannot drive.
#
# Two things the run needs that this script cannot check for:
#   * A Gmail token at ~\.jobtrackeragent\gmail_authorized_user.json. Unverified
#     OAuth apps get 7-day refresh tokens, so an unattended run will eventually
#     hit an expired one; scan-leads reports it and the search half still runs.
#     Re-auth interactively with `main.py gmail-auth`.
#   * A signed-in app.welcometothejungle.com session in Brave. Without it the
#     WTTJ step returns a GraphQL error and those leads stay parked in
#     ~/.proficiently/wttj-pending.json for the next run.

$ErrorActionPreference = 'Stop'
$proj    = 'D:\Code\HelperAgents'
$logDir  = Join-Path $proj 'logs'
$stamp   = Get-Date -Format 'yyyy-MM-dd'
$log     = Join-Path $logDir "job-search-$stamp.log"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# The browser must be up for the automation half of the search: `claude
# --chrome` attaches to a running browser through the extension, it does not
# start one.
$brave = 'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe'
if (-not (Get-Process brave -ErrorAction SilentlyContinue)) {
    Start-Process $brave
    Start-Sleep -Seconds 20
}

Set-Location $proj

$allowed = @(
    'Bash(D:\Code\HelperAgents\venv\Scripts\python.exe *)',
    'Bash(git *)',
    'Read', 'Write', 'Edit', 'Glob', 'Grep',
    'mcp__claude-in-chrome__*'
)

"=== run $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8

# --chrome is what exposes mcp__claude-in-chrome__* to a -p run. Without it the
# browser tools are simply absent -- both the job-search step and the Welcome to
# the Jungle URL resolution die.
& claude -p '/job-tracker search' `
    --chrome `
    --allowedTools $allowed `
    --output-format text 2>&1 |
    Out-File -FilePath $log -Append -Encoding utf8

"=== exit $LASTEXITCODE ===" | Out-File -FilePath $log -Append -Encoding utf8

# The model's prose above is a summary of a summary -- the structured reports
# each stage prints are read by the model and then gone. Every stage also
# appends to ~/.proficiently/run-metrics.jsonl, so replay the funnel into the
# log. This is what tells a thin night from a broken one after the fact: a
# stage that failed shows FAILED with its error, and a stage that never ran at
# all is named under "no record for".
"--- funnel ---" | Out-File -FilePath $log -Append -Encoding utf8
& "$proj\venv\Scripts\python.exe" "$proj\job_tracker_agent\main.py" run-report --days 1 2>&1 |
    Out-File -FilePath $log -Append -Encoding utf8

# Prune logs older than 30 days.
Get-ChildItem $logDir -Filter 'job-search-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force
