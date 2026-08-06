"""Watchdog process for main.py — detects a FROZEN (hung, not crashed) bot
and kills it so it can be restarted.

DIVISION OF LABOR WITH WINDOWS TASK SCHEDULER:
main.py runs as a Task Scheduler task with "restart on failure" enabled.
Task Scheduler can only tell that main.py has failed when its process
actually EXITS (crash, uncaught exception, non-zero exit code) — it cannot
tell the difference between "running normally" and "alive but stuck",
which is exactly the RDP/algo-trading-disabled scenario that motivated this
script in the first place. So:
  - Task Scheduler owns: starting main.py at boot, and restarting it after
    a crash/exit.
  - watchdog.py owns: noticing a HANG (process alive, but logs/app.log has
    gone stale past all reasonable doubt) and killing it — which converts
    the hang into an exit that Task Scheduler's restart-on-failure can then
    pick up.

IMPORTANT — watchdog.py deliberately does NOT try to launch main.py just
because it isn't currently running. An earlier version did, and running
that alongside Task Scheduler's own startup/restart behavior caused FOUR
duplicate main.py processes in one incident. Only after watchdog itself
kills a hung process does it wait briefly and check whether something
(Task Scheduler) has already restarted it before falling back to launching
main.py itself as a last resort. Even then, main.py's own startup duplicate
check (see main.py) is the real backstop against ending up with two copies
running — that check is timing-independent, unlike trying to perfectly
coordinate two separate schedulers.

STALENESS DETECTION: main.py now logs a "[HEARTBEAT]" line to logs/app.log
every 60 seconds regardless of trading activity (see main.py), so a silent
log file now reliably means "stuck", not "no signal happened lately". This
lets the default stale_threshold be much tighter than before.

WHO WATCHES THE WATCHDOG: this script itself can crash too (e.g. it did once,
within 3 seconds of launch, with nothing logged to explain why). Its own
Task Scheduler task ("MT5-Bot-Watchdog") should have TWO triggers — "At
startup" AND "every 5 minutes" — so a crash is noticed and recovered from
automatically rather than leaving the bot unmonitored until someone checks.
The 5-minute repeat trigger would just relaunch a duplicate if the previous
run were still alive and healthy, so main() checks for an existing
watchdog.py process first and exits immediately, harmlessly, if one is
already running (same approach as main.py's own duplicate check). Any crash
is now logged with a full traceback to logs/watchdog.log before exiting, so
it stays diagnosable instead of just going silent.

Run this as its OWN Task Scheduler task (or a second Command Prompt window)
— separate from, and running alongside, the main.py task:
    python scripts\\watchdog.py

Optional tuning flags:
    python scripts\\watchdog.py --check-interval 150 --stale-threshold 180 ^
        --startup-grace 120 --restart-cooldown 120 --post-kill-recheck-delay 60

Stop it with Ctrl+C. Logs to logs/watchdog.log (+ console), separate from
main.py's logs/app.log.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

from bot.process_utils import find_script_process, is_process_name_running, launch_python_script

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
MAIN_SCRIPT_MATCH = "main.py"
THIS_SCRIPT_MATCH = "watchdog.py"
APP_LOG = PROJECT_ROOT / "logs" / "app.log"
WATCHDOG_LOG_DIR = PROJECT_ROOT / "logs"

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


def kill_process(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("taskkill failed for pid=%s: %s", pid, result.stderr.strip() or result.stdout.strip())
        return False
    return True


def is_mt5_terminal_running() -> bool:
    return any(is_process_name_running(name) for name in MT5_TERMINAL_PROCESS_NAMES)


def launch_main_as_fallback() -> int | None:
    """Only called after watchdog's own kill found nothing else took over.
    Still re-verifies MT5 is up and no main.py has appeared in the meantime."""
    if find_script_process(MAIN_SCRIPT_MATCH) is not None:
        logger.info("main.py has appeared since the last check — not launching a duplicate.")
        return None

    if not is_mt5_terminal_running():
        logger.error(
            "MT5 terminal does not appear to be running (no terminal64.exe/terminal.exe process). "
            "Launching main.py now would just fail to connect, so skipping. "
            "Please make sure MT5 is open — will keep checking."
        )
        return None

    return launch_python_script(MAIN_SCRIPT, PROJECT_ROOT)


def check_once(state: dict, args: argparse.Namespace) -> None:
    now = time.time()
    proc = find_script_process(MAIN_SCRIPT_MATCH)

    if proc is None:
        # Not running. Task Scheduler owns starting/restarting main.py now —
        # watchdog only acts here as a fallback immediately after ITS OWN
        # kill (handled inline below), never as a general "it's missing,
        # launch it" reflex, to avoid racing Task Scheduler's own restart.
        state["last_seen_pid"] = None
        logger.info("main.py is not currently running (Task Scheduler is responsible for starting/restarting it).")
        return

    if proc["pid"] != state.get("last_seen_pid"):
        logger.info(
            "Observed main.py pid=%s (previously %s) — treating as a fresh start, resetting startup grace period.",
            proc["pid"], state.get("last_seen_pid"),
        )
        state["main_last_started_at"] = now
        state["last_seen_pid"] = proc["pid"]

    if now - state["main_last_started_at"] < args.startup_grace:
        logger.info(
            "pid=%s within startup grace period (%.0fs / %ds) — skipping staleness check.",
            proc["pid"], now - state["main_last_started_at"], args.startup_grace,
        )
        return

    age = float("inf") if not APP_LOG.exists() else now - APP_LOG.stat().st_mtime

    if age <= args.stale_threshold:
        logger.info("OK: pid=%s, app.log (heartbeat) last updated %.0fs ago.", proc["pid"], age)
        return

    # Frozen — rate-limit repeated kill attempts.
    if state["last_kill_attempt_at"] is not None and (now - state["last_kill_attempt_at"]) < args.restart_cooldown:
        logger.warning(
            "Freeze detected (pid=%s, log age=%.0fs) but restart cooldown still active, waiting.",
            proc["pid"], age,
        )
        return

    logger.critical(
        "FREEZE DETECTED: pid=%s has not logged a heartbeat in %.0fs (threshold %ds). Killing it — "
        "Task Scheduler's restart-on-failure should bring it back up. "
        "Any position already open on MT5 is unaffected by this.",
        proc["pid"], age, args.stale_threshold,
    )
    state["last_kill_attempt_at"] = now

    if not kill_process(proc["pid"]):
        logger.error("Could not confirm pid=%s was killed (it may have already exited on its own).", proc["pid"])
        return

    logger.info(
        "Killed pid=%s. Waiting %ds to give Task Scheduler a chance to restart it before checking.",
        proc["pid"], args.post_kill_recheck_delay,
    )
    time.sleep(args.post_kill_recheck_delay)

    replacement = find_script_process(MAIN_SCRIPT_MATCH)
    if replacement is not None:
        logger.info("Task Scheduler already restarted main.py: pid=%s.", replacement["pid"])
        state["main_last_started_at"] = time.time()
        state["last_seen_pid"] = replacement["pid"]
        return

    logger.warning(
        "Task Scheduler has not restarted main.py within %ds of the kill. "
        "Launching it directly as a fallback — if this keeps happening, check the "
        "main.py task's restart-on-failure setting in Task Scheduler.",
        args.post_kill_recheck_delay,
    )
    new_pid = launch_main_as_fallback()
    if new_pid is not None:
        state["main_last_started_at"] = time.time()
        state["last_seen_pid"] = new_pid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watchdog for main.py — kills it if hung; Task Scheduler handles restarting/crashes.")
    parser.add_argument("--check-interval", type=float, default=150, help="Seconds between checks (default: 150 = 2.5min)")
    parser.add_argument("--stale-threshold", type=float, default=180, help="app.log silence (s) before treating main.py as frozen (default: 180 = 3min = 3 missed heartbeats)")
    parser.add_argument("--startup-grace", type=float, default=120, help="Seconds after a (re)start before staleness checks begin (default: 120 = 2min)")
    parser.add_argument("--restart-cooldown", type=float, default=120, help="Minimum seconds between kill attempts (default: 120 = 2min)")
    parser.add_argument("--post-kill-recheck-delay", type=float, default=60, help="Seconds to wait after a kill before falling back to launching main.py itself (default: 60)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()

    # Everything below is wrapped so a crash — including one during startup,
    # like the duplicate-check call below — gets a full traceback logged to
    # watchdog.log instead of vanishing silently. Task Scheduler is expected
    # to also poll every 5 minutes (in addition to "at startup"), so letting
    # this process actually exit after logging is correct: it'll be relaunched
    # automatically rather than limping along in a broken state.
    try:
        existing = find_script_process(THIS_SCRIPT_MATCH)
        if existing is not None:
            logger.info("Another watchdog.py is already running (pid=%s). Exiting harmlessly.", existing["pid"])
            return

        logger.info(
            "Watchdog starting. check_interval=%ss stale_threshold=%ss startup_grace=%ss "
            "restart_cooldown=%ss post_kill_recheck_delay=%ss",
            args.check_interval, args.stale_threshold, args.startup_grace,
            args.restart_cooldown, args.post_kill_recheck_delay,
        )
        logger.info(
            "Role: hang detection only. Task Scheduler is responsible for starting main.py "
            "and restarting it on crash/exit. Watching: %s", MAIN_SCRIPT,
        )

        state = {"main_last_started_at": time.time(), "last_kill_attempt_at": None, "last_seen_pid": None}

        while True:
            check_once(state, args)
            time.sleep(args.check_interval)

    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user (Ctrl+C).")
    except Exception:
        logger.critical("Watchdog crashed with an unhandled exception:", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
