"""Shared, self-contained helpers for finding/launching OS processes on
Windows via PowerShell/WMI, matched by command-line substring.

Used by main.py (duplicate-instance self-check), scripts/watchdog.py (hang
detection), and api_server.py (status + start/stop) — kept in one place so
all three always agree on exactly what counts as "the main.py process."
Standard library only, no dependency on the rest of the bot/ package, so a
bug elsewhere can't take process supervision down with it.

Multi-account: several main.py (and watchdog.py) processes now run at once,
one per account, distinguished only by a "--account <name>" command-line
argument. find_account_process() is the account-aware counterpart to
find_script_process() — use it wherever "is *this account's* instance
already running" is the question, which is everywhere duplicate-instance
detection matters now.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("bot.process_utils")


def run_powershell(command: str, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("PowerShell process query failed: %s", e)
        return ""
    return result.stdout.strip()


def list_processes(exe_name: str) -> list[dict]:
    """Returns [{"pid": int, "cmdline": str}] for every running process with this exe name."""
    command = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" "
        "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    output = run_powershell(command)
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logger.error("Could not parse process list for %s: %r", exe_name, output[:300])
        return []
    if isinstance(data, dict):  # PowerShell gives an object, not an array, for a single match
        data = [data]
    return [
        {"pid": int(p["ProcessId"]), "cmdline": p.get("CommandLine") or ""}
        for p in data if p.get("ProcessId") is not None
    ]


def find_script_process(script_match: str, exclude_pid: int | None = None) -> dict | None:
    """Finds a running python.exe process whose command line contains
    `script_match` (case-insensitive). Excludes `exclude_pid` (defaults to
    the CALLER's own PID) — critical when main.py itself calls this to
    check for a duplicate: it must never match itself."""
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    for proc in list_processes("python.exe"):
        if proc["pid"] == exclude_pid:
            continue
        if script_match.lower() in proc["cmdline"].lower():
            return proc
    return None


def find_account_process(script_match: str, account: str, exclude_pid: int | None = None) -> dict | None:
    """Like find_script_process(), but also requires the command line to
    carry `--account <account>` as a whole argument — so "demo1" doesn't
    also match a running "demo10" instance. This is what makes it safe for
    multiple accounts' main.py/watchdog.py processes to run side by side:
    each one only ever conflicts with another instance of the SAME account."""
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    account_pattern = re.compile(rf"--account[=\s]+{re.escape(account)}(?:\s|$)", re.IGNORECASE)
    for proc in list_processes("python.exe"):
        if proc["pid"] == exclude_pid:
            continue
        cmdline = proc["cmdline"]
        if script_match.lower() not in cmdline.lower():
            continue
        if account_pattern.search(cmdline):
            return proc
    return None


def is_process_name_running(exe_name: str) -> bool:
    return bool(list_processes(exe_name))


def launch_python_script(script_path: Path, cwd: Path, extra_args: list[str] | None = None) -> int | None:
    try:
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        args = [sys.executable, str(script_path), *(extra_args or [])]
        proc = subprocess.Popen(args, cwd=str(cwd), creationflags=creation_flags)
        logger.info("Launched %s: pid=%s args=%s", script_path.name, proc.pid, extra_args or [])
        return proc.pid
    except Exception:
        logger.exception("Failed to launch %s", script_path.name)
        return None


def scheduled_task_state(task_name: str) -> str | None:
    """The Windows Task Scheduler state of `task_name` -- "Ready", "Running",
    "Disabled", ... -- or None when there is no such task (or not Windows).

    Added 2026-09-22. Bots launched by the gateway were CHILDREN of the
    gateway, and died with it: restarting the gateway that morning killed
    demo2_m3/m5 mid-session. A bot launched through its own scheduled task
    belongs to Windows instead, and nothing the gateway does can touch it.
    """
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", task_name, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        logger.exception("schtasks /Query failed for %s", task_name)
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    rows = list(csv.reader(r.stdout.strip().splitlines()))
    return rows[-1][-1].strip() if rows and rows[-1] else None


def run_scheduled_task(task_name: str) -> bool:
    """Starts `task_name` now. True when Task Scheduler accepted it."""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", task_name],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        logger.exception("schtasks /Run failed for %s", task_name)
        return False
    if r.returncode != 0:
        logger.error("schtasks /Run %s refused: %s", task_name, (r.stderr or r.stdout).strip())
    return r.returncode == 0
