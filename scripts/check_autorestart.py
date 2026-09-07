"""Would a crashed bot actually come back on its own?

On 2026-09-07 two demo1 bots were killed and NEITHER was relaunched --
both accounts sat down until a human noticed. That is the failure mode
that matters most in this project: no trades, no error, no alert, just
silence, for as long as nobody looks.

The intended design (see scripts/watchdog.py's header) is two Task
Scheduler tasks per account:
  - main.py, with "restart on failure", so Task Scheduler owns restarting
    after a crash or exit;
  - watchdog.py, with BOTH an "at startup" and an "every 5 minutes"
    trigger, which notices a HUNG (alive but stuck) bot and kills it,
    converting the hang into an exit that the first task can recover.

This reports what is really configured and really running, per account,
and names the gaps. A bot launched by hand or by the mobile app's start
endpoint is NOT owned by Task Scheduler at all -- killing it triggers no
restart, which is the most likely explanation for what happened.

    python scripts/check_autorestart.py

Read-only: queries Task Scheduler and the process list. Changes nothing.
"""
from __future__ import annotations

import json
import re
import sys

sys.path.insert(0, ".")

from bot.config import discover_configured_accounts
from bot.process_utils import find_account_process, run_powershell

PS_QUERY = r"""
Get-ScheduledTask | Where-Object {
  ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match 'main\.py|watchdog\.py'
} | ForEach-Object {
  $t = $_
  $info = $null
  try { $info = $t | Get-ScheduledTaskInfo } catch {}
  [PSCustomObject]@{
    TaskName        = $t.TaskName
    State           = [string]$t.State
    Command         = (($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' ; ')
    RestartCount    = $t.Settings.RestartCount
    RestartInterval = [string]$t.Settings.RestartInterval
    Triggers        = (($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ',')
    LastRunTime     = [string]$info.LastRunTime
    LastTaskResult  = $info.LastTaskResult
  }
} | ConvertTo-Json -Compress
"""


def scheduled_tasks() -> list[dict]:
    output = run_powershell(PS_QUERY.strip(), timeout=60)
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        print(f"  (could not parse Task Scheduler output: {output[:200]!r})")
        return []
    return [data] if isinstance(data, dict) else data


def task_for(tasks: list[dict], script: str, account: str) -> dict | None:
    pattern = re.compile(rf"--account\s+{re.escape(account)}(\s|$|\")", re.IGNORECASE)
    for t in tasks:
        cmd = t.get("Command") or ""
        if script.lower() in cmd.lower() and pattern.search(cmd):
            return t
    return None


def main() -> None:
    accounts = sorted(discover_configured_accounts())
    tasks = scheduled_tasks()

    print(f"Scheduled tasks mentioning main.py or watchdog.py: {len(tasks)}")
    for t in tasks:
        print(f"  - {t.get('TaskName')}  state={t.get('State')}  "
              f"restartCount={t.get('RestartCount')} interval={t.get('RestartInterval') or 'none'}  "
              f"lastResult={t.get('LastTaskResult')}")
    print()

    problems: list[str] = []

    for account in accounts:
        print("=" * 78)
        print(account)
        print("=" * 78)

        bot_proc = find_account_process("main.py", account)
        wd_proc = find_account_process("watchdog.py", account)
        bot_task = task_for(tasks, "main.py", account)
        wd_task = task_for(tasks, "watchdog.py", account)

        print(f"  main.py process   : {'pid=' + str(bot_proc['pid']) if bot_proc else 'NOT RUNNING'}")
        print(f"  watchdog process  : {'pid=' + str(wd_proc['pid']) if wd_proc else 'NOT RUNNING'}")

        if bot_task is None:
            print("  main.py task      : NONE <-- Task Scheduler does not own this bot, so")
            print("                      nothing will restart it if it crashes or is killed.")
            problems.append(f"{account}: no Task Scheduler task for main.py")
        else:
            restarts = bot_task.get("RestartCount")
            print(f"  main.py task      : {bot_task['TaskName']}  state={bot_task.get('State')}  "
                  f"restartCount={restarts}  interval={bot_task.get('RestartInterval') or 'none'}")
            if not restarts:
                print("                      <-- restart-on-failure is NOT set: a crash will not")
                print("                      be recovered automatically.")
                problems.append(f"{account}: main.py task has no restart-on-failure")

        if wd_task is None:
            print("  watchdog task     : NONE <-- a HUNG bot (alive but stuck) will never be")
            print("                      noticed. This is the RDP/AutoTrading-off scenario.")
            problems.append(f"{account}: no Task Scheduler task for watchdog.py")
        else:
            triggers = wd_task.get("Triggers") or ""
            print(f"  watchdog task     : {wd_task['TaskName']}  state={wd_task.get('State')}  "
                  f"triggers={triggers or 'unknown'}")
            if "Time" not in triggers and "Registration" not in triggers:
                print("                      <-- no repeating trigger: if the watchdog itself dies")
                print("                      it never comes back (see watchdog.py's header).")
                problems.append(f"{account}: watchdog task has no repeating trigger")

        if bot_proc and not wd_proc:
            print("  ! bot is running UNWATCHED -- a hang would go unnoticed.")
            problems.append(f"{account}: bot running with no watchdog process")
        print()

    print("=" * 78)
    if problems:
        print(f"{len(problems)} problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        print()
        print("A bot launched by hand or by the mobile app's start endpoint is not owned by")
        print("Task Scheduler, so killing it triggers no restart. That matches what happened")
        print("on 2026-09-07: both demo1 legs were killed and neither came back.")
    else:
        print("No problems found: every account has a restart-capable task and a live watchdog.")


if __name__ == "__main__":
    main()
