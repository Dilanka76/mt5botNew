"""Show -- and optionally tighten -- how long a dead bot stays dead.

Recovery in this project comes from each main.py task's REPEATING
trigger, not from restart-on-failure: restart-on-failure only applies to
a process the task itself started, and every bot currently running was
started by boot, by the mobile app, or by hand. The repeating trigger
re-runs main.py on a schedule and main.py's own duplicate check makes
that a harmless no-op while one is alive (it exits with code 1, which is
why lastResult=1 on a healthy task is normal here).

So the repetition interval IS the worst-case downtime. demo1's is 30
minutes (visible in logs/demo1_m3/app.log as the :13/:43 pattern): a bot
dying at 09:14 stays dead until 09:43. On M1 that is roughly 30 missed
signals, and on a live account 30 minutes of an open position with
nothing managing it -- no stop of any kind lives at the broker except on
a TP-runner lock.

Shortening it is close to free precisely BECAUSE of the duplicate check:
a 5-minute trigger on a healthy bot just starts python, sees the running
process, logs one line and exits.

    python scripts/set_restart_interval.py                 # show only
    python scripts/set_restart_interval.py --apply         # set to 5 minutes
    python scripts/set_restart_interval.py --apply --minutes 10

Shows by default; changes nothing without --apply. Never touches the
bots themselves -- Task Scheduler settings only, so no trade and no
running process is affected.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys

sys.path.insert(0, ".")

from bot.process_utils import run_powershell

SHOW = r"""
Get-ScheduledTask | Where-Object {
  ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match 'main\.py'
} | ForEach-Object {
  $t = $_
  $reps = @($t.Triggers | ForEach-Object { if ($_.Repetition -and $_.Repetition.Interval) { [string]$_.Repetition.Interval } else { 'none' } })
  [PSCustomObject]@{
    TaskName = $t.TaskName
    State    = [string]$t.State
    Command  = (($t.Actions | ForEach-Object { $_.Arguments }) -join ' ; ')
    Repeats  = ($reps -join ',')
  }
} | ConvertTo-Json -Compress
"""

APPLY = """
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{name}'
$changed = $false
foreach ($trig in $task.Triggers) {{
  if ($trig.Repetition -and $trig.Repetition.Interval) {{
    $trig.Repetition.Interval = '{interval}'
    $changed = $true
  }}
}}
if ($changed) {{
  Set-ScheduledTask -TaskName '{name}' -Trigger $task.Triggers | Out-Null
  Write-Output 'OK'
}} else {{
  Write-Output 'NO_REPEATING_TRIGGER'
}}
"""


def run_ps(command: str, timeout: int = 60) -> tuple[str, str, int]:
    """Like bot.process_utils.run_powershell, but keeps stderr. The shared
    helper returns stdout only, which turned a real Set-ScheduledTask
    error into a bare "NO OUTPUT" and hid the actual reason."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return "", str(exc), -1
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - not Windows, or the call is unavailable
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="actually change the interval")
    p.add_argument("--minutes", type=int, default=5, help="new repeat interval (default 5)")
    p.add_argument("--tasks", default="", help="comma-separated task names; default = every enabled main.py task")
    return p.parse_args()


def tasks() -> list[dict]:
    out = run_powershell(SHOW.strip(), timeout=60)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(f"could not parse: {out[:200]!r}")
        return []
    return [data] if isinstance(data, dict) else data


def main() -> None:
    args = parse_args()
    found = tasks()
    if not found:
        print("No main.py scheduled tasks found.")
        return

    wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
    print(f"{'task':<28}{'state':<12}{'repeat interval':<18}account")
    targets = []
    for t in found:
        name, state = t.get("TaskName", "?"), t.get("State", "?")
        repeats = t.get("Repeats", "none")
        cmd = t.get("Command") or ""
        account = cmd.split("--account")[-1].strip().split()[0] if "--account" in cmd else "?"
        print(f"{name:<28}{state:<12}{repeats:<18}{account}")
        if state == "Disabled":
            continue
        if wanted and name not in wanted:
            continue
        if "none" not in repeats or repeats != "none":
            targets.append(name)

    if not args.apply:
        print()
        print(f"Showing only. Worst-case downtime after a crash = the repeat interval above.")
        print(f"Re-run with --apply to set every enabled task to PT{args.minutes}M.")
        return

    interval = f"PT{args.minutes}M"
    if not is_admin():
        print("\nNOT RUNNING AS ADMINISTRATOR -- Set-ScheduledTask will almost certainly be")
        print("refused. Close this window, reopen PowerShell with 'Run as administrator',")
        print("cd back here and run this again. Attempting anyway so the real error shows:")
    print(f"\nSetting repeat interval to {interval} on {len(targets)} task(s)...")
    failures = 0
    for name in targets:
        out, err, code = run_ps(APPLY.format(name=name, interval=interval).strip())
        if out.strip() == "OK":
            print(f"  {name:<28}OK")
            continue
        failures += 1
        detail = out.strip() or err.strip().splitlines()[0] if (out.strip() or err.strip()) else f"exit code {code}"
        print(f"  {name:<28}FAILED: {detail}")
    if failures:
        print(f"\n{failures} task(s) unchanged. Nothing was half-applied -- each task is set")
        print("in a single call, so a failure leaves that task exactly as it was.")

    print("\nRe-run without --apply to confirm, then:")
    print("    python scripts/check_autorestart.py")


if __name__ == "__main__":
    main()
