"""Is the TP-runner actually LIVE, or only written to a config file?

Editing config/settings.<account>.yaml changes nothing on its own. A
running bot read its config once, at startup, and holds it in memory --
so a setting can be correct on disk and absent from the process that is
actually trading. This checks the running process, not the file.

Three checks per account:
  1. the config on disk has the setting;
  2. a bot process for that account is actually running;
  3. that process STARTED AFTER the config file was last modified --
     which is the only real proof it loaded the new value.

Then it prints the account's most recent "Bot started" log line, which
now states the runner settings the process actually loaded (main.py).
That line is the ground truth: it was written by the process itself.

    python scripts/verify_tp_runner_live.py

Read-only: reads config, process list and log files. Opens no MT5
connection and touches no trading state.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.process_utils import run_powershell

ACCOUNTS = ("demo1_m1", "demo1_m3")
CONTROLS = ("demo2_m1", "demo2_m3")


def process_for(account: str) -> dict | None:
    """Running python.exe carrying --account <account>, with its start time.
    Uses CreationDate, which list_processes() does not expose."""
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
        "| Select-Object ProcessId,CommandLine,CreationDate | ConvertTo-Json -Compress"
    )
    output = run_powershell(command)
    if not output:
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = [data]
    pattern = re.compile(rf"--account\s+{re.escape(account)}(\s|$)", re.IGNORECASE)
    for proc in data:
        cmdline = proc.get("CommandLine") or ""
        if "main.py" in cmdline.lower() and pattern.search(cmdline):
            created = proc.get("CreationDate")
            started = None
            if isinstance(created, str):
                # PowerShell ConvertTo-Json renders CIM dates as /Date(ms)/
                m = re.search(r"/Date\((\d+)", created)
                if m:
                    started = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
                else:
                    try:
                        started = datetime.fromisoformat(created)
                    except ValueError:
                        started = None
            return {"pid": int(proc["ProcessId"]), "started": started}
    return None


def last_started_line(account: str) -> str | None:
    path = PROJECT_ROOT / "logs" / account / "app.log"
    if not path.exists():
        path = PROJECT_ROOT / "logs" / "app.log"
    if not path.exists():
        return None
    found = None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "Bot started:" in line and f"account={account}" in line:
                found = line.strip()
    return found


def main() -> None:
    all_ok = True

    for account in ACCOUNTS:
        account = validate_account_name(account)
        print("=" * 78)
        print(account)
        print("=" * 78)

        config = load_config(account)
        cfg_path = PROJECT_ROOT / f"config/settings.{account}.yaml"
        cfg_mtime = datetime.fromtimestamp(cfg_path.stat().st_mtime, tz=timezone.utc)

        # 1. on disk
        on_disk = config.tp_runner_trail_usd is not None
        print(f"  1. config on disk    : tp_runner_trail_usd={config.tp_runner_trail_usd} "
              f"{'OK' if on_disk else '<-- NOT SET'}")
        print(f"     (breakeven {config.breakeven_trigger_usd}, TP ${config.take_profit_usd:.2f}, "
              f"stop ${config.stop_loss_usd:.2f}, file modified {cfg_mtime:%Y-%m-%d %H:%M:%S} UTC)")
        all_ok &= on_disk

        # 2. running
        proc = process_for(account)
        if proc is None:
            print("  2. running process   : NONE FOUND <-- the bot is not running for this account")
            all_ok = False
            print()
            continue
        print(f"  2. running process   : pid={proc['pid']} OK")

        # 3. started after the config changed -- the only real proof
        if proc["started"] is None:
            print("  3. picked up config? : could not read the process start time; "
                  "rely on the log line below")
        elif proc["started"] > cfg_mtime:
            print(f"  3. picked up config? : YES -- started {proc['started']:%Y-%m-%d %H:%M:%S} UTC, "
                  f"after the config was written")
        else:
            print(f"  3. picked up config? : NO <-- started {proc['started']:%Y-%m-%d %H:%M:%S} UTC, "
                  f"BEFORE the config was written. This process is still running the OLD "
                  f"settings. RESTART IT.")
            all_ok = False

        line = last_started_line(account)
        print(f"  4. what the process itself logged at startup:")
        print(f"     {line if line else '(no \"Bot started\" line found in the log)'}")
        print()

    print("=" * 78)
    print("Controls -- these MUST stay off")
    print("=" * 78)
    for account in CONTROLS:
        c = load_config(account)
        state = "OFF (correct)" if c.tp_runner_trail_usd is None else f"<-- ON ({c.tp_runner_trail_usd}) -- SHOULD BE OFF"
        if c.tp_runner_trail_usd is not None:
            all_ok = False
        print(f"  {account}: tp_runner_trail_usd={c.tp_runner_trail_usd}  {state}")

    print()
    print("ALL CHECKS PASSED -- the TP-runner is live on demo1." if all_ok
          else "SOMETHING IS NOT RIGHT -- see the lines marked '<--' above.")


if __name__ == "__main__":
    main()
