"""Watchdog process for main.py — detects a frozen or dead bot and restarts it.

WHY THIS EXISTS: main.py can go silent while its process stays alive — e.g.
an RDP session change can trigger MT5's "disable algo trading on
profile/account change" setting, after which any MT5 API call the bot makes
hangs forever. Task Manager still shows python.exe running, but nothing new
gets logged. This script runs independently of main.py and watches for that.

WHAT IT DOES, every check_interval seconds:
  1. Looks for a running python.exe process whose command line contains
     "main.py" (via PowerShell/WMI — this is Windows-only, like the rest of
     this bot).
  2. If none is found -> launches main.py.
  3. If one is found but logs/app.log hasn't been modified in over
     stale_threshold seconds (and we're past the startup grace period since
     its last (re)start) -> treats it as frozen, force-kills that specific
     PID, and relaunches main.py.
  4. Before relaunching, checks that the MT5 terminal process is actually
     running — if it isn't, relaunching main.py would just fail to connect,
     so it logs that clearly and waits rather than looping restart attempts.
  5. Restart attempts (successful or not) are rate-limited to at most one
     per restart_cooldown seconds.

This script is intentionally self-contained (Python standard library only,
no dependency on the bot/ package) so a bug in the bot's own code can never
take the watchdog down with it.

WHAT IT DELIBERATELY DOES NOT DO: touch MT5 or any open position. Positions
live on the broker/MT5 side regardless of whether our Python process is
running — restarting main.py just resumes monitoring; it does not open or
close any trade itself.

USAGE (run in a second Command Prompt window, alongside `python main.py` in
the first):
    python scripts\\watchdog.py

Optional tuning flags (defaults match the ranges discussed):
    python scripts\\watchdog.py --check-interval 150 --stale-threshold 300 ^
        --startup-grace 300 --restart-cooldown 120

Stop it with Ctrl+C. Its own activity is logged to logs/watchdog.log (and
the console) — separate from main.py's logs/app.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
MAIN_SCRIPT_MATCH = "main.py"  # substring matched (case-insensitive) against process command lines
APP_LOG = PROJECT_ROOT / "logs" / "app.log"
WATCHDOG_LOG_DIR = PROJECT_ROOT / "logs"
PYTHON_EXE = sys.executable

MT5_TERMINAL_PROCESS_NAMES = ("terminal64.exe", "terminal.exe")

logger = logging.getLogger("watchdog")


def setup_logging() -> None:
    WATCHDOG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        WATCHDOG_LOG_DIR / "watchdog.log", maxBytes=2_000_000, backupCount=3
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def _run_powershell(command: str, timeout: int = 20) -> str:
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
    output = _run_powershell(command)
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        logger.error("Could not parse process list for %s: %r", exe_name, output[:300])
        return []
    if isinstance(data, dict):  # PowerShell ConvertTo-Json gives an object, not an array, for a single match
        data = [data]
    return [
        {"pid": int(p["ProcessId"]), "cmdline": p.get("CommandLine") or ""}
        for p in data if p.get("ProcessId") is not None
    ]


def find_main_process() -> dict | None:
    """Finds the running main.py process, if any. Never matches the watchdog's own PID."""
    for proc in list_processes("python.exe"):
        if proc["pid"] == os.getpid():
            continue
        if MAIN_SCRIPT_MATCH in proc["cmdline"].lower():
            return proc
    return None


def is_mt5_terminal_running() -> bool:
    return any(list_processes(name) for name in MT5_TERMINAL_PROCESS_NAMES)


def kill_process(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("taskkill failed for pid=%s: %s", pid, result.stderr.strip() or result.stdout.strip())
        return False
    return True


def relaunch_main() -> subprocess.Popen | None:
    if not is_mt5_terminal_running():
        logger.error(
            "MT5 terminal does not appear to be running (no terminal64.exe/terminal.exe process). "
            "Relaunching main.py would just fail to connect, so skipping this attempt. "
            "Please make sure MT5 is open — will try again next cycle."
        )
        return None

    try:
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        proc = subprocess.Popen(
            [PYTHON_EXE, str(MAIN_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            creationflags=creation_flags,
        )
        logger.info("Relaunched main.py in a new console window: pid=%s", proc.pid)
        return proc
    except Exception:
        logger.exception("Failed to relaunch main.py")
        return None


def check_once(state: dict, args: argparse.Namespace) -> None:
    now = time.time()
    proc = find_main_process()

    if proc is None:
        # Not running at all — act immediately (no grace period), but still
        # rate-limit repeated attempts via the restart cooldown.
        if state["last_restart_attempt_at"] is not None and (now - state["last_restart_attempt_at"]) < args.restart_cooldown:
            logger.info("main.py is not running; restart cooldown still active, waiting.")
            return
        logger.warning("main.py is not running. Launching it.")
        new_proc = relaunch_main()
        state["last_restart_attempt_at"] = now
        if new_proc is not None:
            state["main_last_started_at"] = now
        return

    # A process is running — only check for a freeze once past the startup
    # grace period since its (assumed) last start, to avoid false positives
    # right after boot/reconnect before it's logged anything yet.
    if now - state["main_last_started_at"] < args.startup_grace:
        logger.info(
            "pid=%s within startup grace period (%.0fs / %ds) — skipping staleness check.",
            proc["pid"], now - state["main_last_started_at"], args.startup_grace,
        )
        return

    age = float("inf") if not APP_LOG.exists() else now - APP_LOG.stat().st_mtime

    if age <= args.stale_threshold:
        logger.info("OK: pid=%s, app.log last updated %.0fs ago.", proc["pid"], age)
        return

    # Frozen.
    if state["last_restart_attempt_at"] is not None and (now - state["last_restart_attempt_at"]) < args.restart_cooldown:
        logger.warning(
            "Freeze detected (pid=%s, log age=%.0fs) but restart cooldown still active, waiting.",
            proc["pid"], age,
        )
        return

    logger.critical(
        "FREEZE DETECTED: pid=%s has not written to app.log in %.0fs (threshold %ds). Killing and relaunching. "
        "Note: any position already open on MT5 is unaffected by this restart.",
        proc["pid"], age, args.stale_threshold,
    )
    if kill_process(proc["pid"]):
        logger.info("Killed frozen process pid=%s", proc["pid"])
    else:
        logger.error("Could not confirm pid=%s was killed (it may have already exited on its own).", proc["pid"])

    new_proc = relaunch_main()
    state["last_restart_attempt_at"] = now
    if new_proc is not None:
        state["main_last_started_at"] = now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watchdog for main.py — restarts it if frozen or not running.")
    parser.add_argument("--check-interval", type=float, default=150, help="Seconds between checks (default: 150 = 2.5min)")
    parser.add_argument("--stale-threshold", type=float, default=300, help="app.log silence (s) before treating main.py as frozen (default: 300 = 5min)")
    parser.add_argument("--startup-grace", type=float, default=300, help="Seconds after a (re)start before staleness checks begin (default: 300 = 5min)")
    parser.add_argument("--restart-cooldown", type=float, default=120, help="Minimum seconds between restart attempts (default: 120 = 2min)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()

    logger.info(
        "Watchdog starting. check_interval=%ss stale_threshold=%ss startup_grace=%ss restart_cooldown=%ss",
        args.check_interval, args.stale_threshold, args.startup_grace, args.restart_cooldown,
    )
    logger.info("Watching: %s (via process command line containing '%s')", MAIN_SCRIPT, MAIN_SCRIPT_MATCH)

    state = {"main_last_started_at": time.time(), "last_restart_attempt_at": None}

    existing = find_main_process()
    if existing is not None:
        logger.info("main.py already running at startup: pid=%s. Applying startup grace period before first check.", existing["pid"])
    check_once(state, args)  # immediate check at boot, mainly to catch "forgot to start it"

    try:
        while True:
            time.sleep(args.check_interval)
            check_once(state, args)
    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user (Ctrl+C).")


if __name__ == "__main__":
    main()