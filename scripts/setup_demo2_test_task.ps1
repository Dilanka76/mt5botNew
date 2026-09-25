# One-off setup: registers the Scheduled Task for demo2_test -- the user's
# own EMA5/9 M1 strategy, running on the demo2 login so he can watch it.
#
# Same shape as every other bot task here (setup_demo2_tasks.ps1): a boot
# trigger plus a 30-minute repetition, so it restarts itself if it dies
# and comes back after a reboot. main.py takes a single-instance lock, so
# a repeat firing while it is already running is harmless.
#
# THE WATCHDOG IS DELIBERATELY NOT REGISTERED. Every other account has
# one; this is a measurement running on demo money, and a watchdog
# restarting it mid-test would quietly change what is being measured.
# If it dies, that is worth seeing in the trade record.
#
# BEFORE RUNNING: demo2_m3 and demo2_m5 must stay stopped and disabled.
# They share this MT5 login and their reject_manual_trades would close
# this bot's trades on sight.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_demo2_test_task.ps1
$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$workDir = "C:\MT5BOTSCRIPT\mt5bot"
$account = "demo2_test"

$cred = Get-Credential -Message "Windows account + password to run the demo2_test scheduled task as"

$botAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "main.py --account $account" -WorkingDirectory $workDir
$botBootTrigger = New-ScheduledTaskTrigger -AtStartup
$botTimeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "MT5-Bot-$account" -Action $botAction -Trigger @($botBootTrigger, $botTimeTrigger) -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
Write-Output "Registered MT5-Bot-$account"

Write-Output ""
Write-Output "=== Verification ==="
Get-ScheduledTask -TaskName "MT5-Bot-$account" | ForEach-Object {
    Write-Output "TaskName: $($_.TaskName)  State: $($_.State)"
    $_.Triggers | ForEach-Object { Write-Output "  TriggerType: $($_.CimClass.CimClassName)  RepetitionInterval: $($_.Repetition.Interval)" }
}

Write-Output ""
Write-Output "The other two demo2 bots must stay disabled -- they share this login:"
Get-ScheduledTask -TaskName "MT5-Bot-demo2_m3", "MT5-Bot-demo2_m5" -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Output "  $($_.TaskName): $($_.State)" }
