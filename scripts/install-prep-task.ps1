# Registers the daily prep run as a Windows Scheduled Task.
# Runs at 02:00, an hour after ClaudeDailyJobSearch, so the jobs that search
# adds to "to apply" are there to be prepared.

$taskName = 'ClaudeDailyPrep'
$script   = 'D:\Code\HelperAgents\scripts\daily-prep.ps1'
$runAt    = '02:00'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory 'D:\Code\HelperAgents'

$trigger = New-ScheduledTaskTrigger -Daily -At $runAt

# Interactive: prep shells out to `claude`, which needs the logged-on user's
# OAuth/keychain state. It will NOT run while logged out.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# 5h backstop: prep's own ceiling is 4h plus retry backoff, and Python should
# be the thing that stops the run, not the scheduler.
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 5) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Runs job_tracker_agent prep once a day, after the daily search.' `
    -Force

Write-Host "Registered '$taskName' for $runAt daily."
Write-Host "Test now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
