# One-off setup: registers a Windows Scheduled Task that runs
# scripts/generate_live_test_report.py once a day at 23:55 Asia/Colombo
# time, regenerating reports/live_test/live_test_report.xlsx fresh each
# time (the script itself is idempotent -- always rewrites the whole file
# from source logs, never appends/duplicates).
#
# Deliberately does NOT assume the server's own OS timezone is Colombo --
# bot/sessions.py explicitly computes session windows from UTC rather than
# trusting the server's local clock "regardless of the EC2 instance's OS
# timezone setting" (see feedback_windows_restart_gotchas.md), so this
# script converts 23:55 Colombo -> the server's actual local timezone via
# .NET's TimeZoneInfo instead of hardcoding "23:55" as a local trigger
# time, which would silently fire at the wrong moment if the server isn't
# already set to Colombo time.
$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$workDir = "C:\MT5BOTSCRIPT\mt5bot"
$taskName = "MT5-LiveTestReport"

$colomboTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Sri Lanka Standard Time")
$todayColombo = [System.TimeZoneInfo]::ConvertTime((Get-Date), $colomboTz).Date
$triggerColombo = [System.DateTime]::SpecifyKind($todayColombo.AddHours(23).AddMinutes(55), [System.DateTimeKind]::Unspecified)
$triggerLocal = [System.TimeZoneInfo]::ConvertTime($triggerColombo, $colomboTz, [System.TimeZoneInfo]::Local)

Write-Output "Server local timezone: $([System.TimeZoneInfo]::Local.Id)"
Write-Output "23:55 Asia/Colombo -> $($triggerLocal.ToString('HH:mm')) server-local time -- scheduling daily at that local time"

$cred = Get-Credential -UserName "trader" -Message "Password to register the $taskName scheduled task"

$action = New-ScheduledTaskAction -Execute $pythonExe -Argument "scripts\generate_live_test_report.py" -WorkingDirectory $workDir
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerLocal.ToString('HH:mm')
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
Write-Output "Registered $taskName"

Write-Output ""
Write-Output "=== Verification ==="
$t = Get-ScheduledTask -TaskName $taskName
Write-Output "TaskName: $($t.TaskName)  State: $($t.State)"
$t.Triggers | ForEach-Object { Write-Output "  TriggerType: $($_.CimClass.CimClassName)  StartBoundary: $($_.StartBoundary)" }
Write-Output ""
Write-Output "To run it immediately instead of waiting for 23:55 Colombo: Start-ScheduledTask -TaskName '$taskName'"
