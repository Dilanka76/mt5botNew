# One-off setup: registers the Windows Scheduled Tasks for the new
# demo1_ce pseudo-account, matching demo1_m1/demo1_m3's exact task shape
# (same python.exe path, same working directory, same principal) but with
# the CORRECT 30-minute repetition on the main bot task from the start
# (see feedback_windows_restart_gotchas.md Trap 8/9 — the old default was
# a mistaken 5-minute interval that had to be fixed after the fact for the
# other two accounts). The watchdog task stays at 5 minutes, matching the
# other watchdogs' actual hang-detection polling cadence.
$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$workDir = "C:\MT5BOTSCRIPT\mt5bot"

$cred = Get-Credential -UserName "trader" -Message "Password to register demo1_ce scheduled tasks"
$principal = New-ScheduledTaskPrincipal -UserId "trader" -LogonType Password -RunLevel Highest

# --- Main bot task: 30-minute repetition from the start ---
$botAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "main.py --account demo1_ce" -WorkingDirectory $workDir
$botBootTrigger = New-ScheduledTaskTrigger -AtStartup
$botTimeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "MT5-Bot-demo1_ce" -Action $botAction -Trigger @($botBootTrigger, $botTimeTrigger) -Principal $principal -User $cred.UserName -Password $cred.GetNetworkCredential().Password -Force | Out-Null
Write-Output "Registered MT5-Bot-demo1_ce"

# --- Watchdog task: 5-minute repetition, matching the other watchdogs ---
$wdAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "scripts\watchdog.py --account demo1_ce" -WorkingDirectory $workDir
$wdBootTrigger = New-ScheduledTaskTrigger -AtStartup
$wdTimeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "MT5-Bot-Watchdog-demo1_ce" -Action $wdAction -Trigger @($wdBootTrigger, $wdTimeTrigger) -Principal $principal -User $cred.UserName -Password $cred.GetNetworkCredential().Password -Force | Out-Null
Write-Output "Registered MT5-Bot-Watchdog-demo1_ce"

Write-Output ""
Write-Output "=== Verification ==="
Get-ScheduledTask -TaskName "MT5-Bot-demo1_ce", "MT5-Bot-Watchdog-demo1_ce" | ForEach-Object {
    $t = $_
    Write-Output "----"
    Write-Output "TaskName: $($t.TaskName)  State: $($t.State)"
    $t.Triggers | ForEach-Object { Write-Output "  TriggerType: $($_.CimClass.CimClassName)  RepetitionInterval: $($_.Repetition.Interval)" }
}
