# Registers the daily job search as a Windows Scheduled Task.
# Run once, from an elevated OR normal PowerShell (normal is fine for a
# user-scope task).

$taskName = 'ClaudeDailyJobSearch'
$script   = 'D:\Code\HelperAgents\scripts\daily-job-search.ps1'
$runAt    = '01:00'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory 'D:\Code\HelperAgents'

$trigger = New-ScheduledTaskTrigger -Daily -At $runAt

# InteractiveToken: the task needs the logged-on desktop session so it can
# drive the already-running Chrome. It will NOT run while logged out.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Runs /job-tracker search once a day via Claude Code headless.' `
    -Force

Write-Host "Registered '$taskName' for $runAt daily."
Write-Host "Test now:  Start-ScheduledTask -TaskName $taskName"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
