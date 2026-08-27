# One-off setup: registers the Windows Scheduled Tasks for the new
# demo2_m1/demo2_m3 pseudo-accounts, matching demo1_m1/demo1_m3's exact
# task shape (main bot task with 30-minute repetition + boot trigger,
# watchdog task with 5-minute repetition + boot trigger, same python.exe
# path, same working directory).
$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
$workDir = "C:\MT5BOTSCRIPT\mt5bot"
$accounts = @("demo2_m1", "demo2_m3")

$cred = Get-Credential -Message "Windows account + password to run the demo2_m1/demo2_m3 scheduled tasks as"

foreach ($account in $accounts) {
    # --- Main bot task: 30-minute repetition from the start ---
    $botAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "main.py --account $account" -WorkingDirectory $workDir
    $botBootTrigger = New-ScheduledTaskTrigger -AtStartup
    $botTimeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName "MT5-Bot-$account" -Action $botAction -Trigger @($botBootTrigger, $botTimeTrigger) -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
    Write-Output "Registered MT5-Bot-$account"

    # --- Watchdog task: 5-minute repetition ---
    $wdAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "scripts\watchdog.py --account $account" -WorkingDirectory $workDir
    $wdBootTrigger = New-ScheduledTaskTrigger -AtStartup
    $wdTimeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName "MT5-Bot-Watchdog-$account" -Action $wdAction -Trigger @($wdBootTrigger, $wdTimeTrigger) -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
    Write-Output "Registered MT5-Bot-Watchdog-$account"
}

Write-Output ""
Write-Output "=== Verification ==="
Get-ScheduledTask -TaskName "MT5-Bot-demo2_m1", "MT5-Bot-Watchdog-demo2_m1", "MT5-Bot-demo2_m3", "MT5-Bot-Watchdog-demo2_m3" | ForEach-Object {
    $t = $_
    Write-Output "----"
    Write-Output "TaskName: $($t.TaskName)  State: $($t.State)"
    $t.Triggers | ForEach-Object { Write-Output "  TriggerType: $($_.CimClass.CimClassName)  RepetitionInterval: $($_.Repetition.Interval)" }
}
