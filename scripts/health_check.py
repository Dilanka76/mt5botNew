"""Fast health snapshot across all 5 trading accounts — one command instead
of manually checking each account's process list and logs.

For each account (demo1, demo2, live1, live2, live3) this checks:
  - is `main.py --account <name>` running (bot.process_utils, same
    account-aware process matching main.py/watchdog.py use for their own
    duplicate-instance checks)
  - is `scripts/watchdog.py --account <name>` running
  - how old is the last "[HEARTBEAT]" line in logs/<name>/app.log
  - how old is the last line in logs/<name>/watchdog.log

An account is UNHEALTHY if its main.py isn't running, its watchdog.py
isn't running, or its last heartbeat is older than
STALE_HEARTBEAT_THRESHOLD_SECONDS (matches watchdog.py's own default
--stale-threshold of 180s = 3 missed heartbeats).

Pure stdlib + bot.process_utils (also pure stdlib) — no MT5 import, so this
can run standalone without a live MT5 connection.

Usage:
    python scripts/health_check.py

Exit code 0 if all 5 accounts are healthy, 1 if any are unhealthy — so this
can be wired into an external monitor/cron later if wanted.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Must come before the bot.* import — same reasoning as scripts/watchdog.py:
# not guaranteed to be run with the project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.process_utils import find_account_process

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = ["demo1", "demo2", "live1", "live2", "live3"]
MAIN_SCRIPT_MATCH = "main.py"
WATCHDOG_SCRIPT_MATCH = "watchdog.py"
STALE_HEARTBEAT_THRESHOLD_SECONDS = 180  # 3 minutes = 3 missed heartbeats, matches watchdog.py's default


def _parse_log_timestamp(line: str) -> datetime | None:
    """Log lines start "YYYY-MM-DD HH:MM:SS,mmm [LEVEL] ..." (see
    bot/logging_setup/logger.py and scripts/watchdog.py's own formatters) —
    that prefix is always exactly 23 characters."""
    try:
        return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def _last_timestamp(path: Path, must_contain: str | None = None) -> datetime | None:
    """Last line's timestamp in a log file, scanning from the end.
    `must_contain` restricts which lines count (e.g. only "[HEARTBEAT]"
    lines in app.log, not just any log line)."""
    if not path.exists():
        return None
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if must_contain and must_contain not in line:
            continue
        ts = _parse_log_timestamp(line)
        if ts is not None:
            return ts
    return None


def _format_age(ts: datetime | None) -> str:
    if ts is None:
        return "no data"
    age_seconds = max(0.0, (datetime.now() - ts).total_seconds())
    if age_seconds < 60:
        return f"{age_seconds:.0f}s"
    return f"{age_seconds / 60:.1f}m"


@dataclass
class AccountHealth:
    account: str
    main_running: bool
    watchdog_running: bool
    heartbeat_ts: datetime | None
    watchdog_log_ts: datetime | None

    @property
    def heartbeat_age_seconds(self) -> float | None:
        if self.heartbeat_ts is None:
            return None
        return (datetime.now() - self.heartbeat_ts).total_seconds()

    @property
    def healthy(self) -> bool:
        if not self.main_running or not self.watchdog_running:
            return False
        age = self.heartbeat_age_seconds
        return age is not None and age <= STALE_HEARTBEAT_THRESHOLD_SECONDS


def check_account(account: str) -> AccountHealth:
    main_proc = find_account_process(MAIN_SCRIPT_MATCH, account)
    watchdog_proc = find_account_process(WATCHDOG_SCRIPT_MATCH, account)

    app_log = PROJECT_ROOT / "logs" / account / "app.log"
    watchdog_log = PROJECT_ROOT / "logs" / account / "watchdog.log"

    return AccountHealth(
        account=account,
        main_running=main_proc is not None,
        watchdog_running=watchdog_proc is not None,
        heartbeat_ts=_last_timestamp(app_log, must_contain="[HEARTBEAT]"),
        watchdog_log_ts=_last_timestamp(watchdog_log),
    )


def print_summary(results: list[AccountHealth]) -> None:
    headers = ["Account", "Main Process", "Watchdog Process", "Last Heartbeat Age", "Watchdog Log Age", "Status"]
    rows = [
        [
            r.account,
            "RUNNING" if r.main_running else "NOT RUNNING",
            "RUNNING" if r.watchdog_running else "NOT RUNNING",
            _format_age(r.heartbeat_ts),
            _format_age(r.watchdog_log_ts),
            "HEALTHY" if r.healthy else "UNHEALTHY",
        ]
        for r in results
    ]

    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]

    def fmt_row(cols: list[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def main() -> int:
    results = [check_account(account) for account in ACCOUNTS]
    print_summary(results)

    unhealthy = [r for r in results if not r.healthy]
    print()
    if unhealthy:
        print(f"{len(unhealthy)} account(s) unhealthy: {', '.join(r.account for r in unhealthy)}")
        return 1

    print("All 5 accounts healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())