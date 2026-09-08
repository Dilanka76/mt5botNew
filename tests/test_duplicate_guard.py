"""main.py's duplicate-instance guard must FAIL CLOSED.

    python3 tests/test_duplicate_guard.py

On 2026-09-08 two Task Scheduler triggers fired in the same second and
both demo2 legs started a SECOND copy of main.py. Each copy had its own
idea of being flat, so each opened a position -- four simultaneous trades
on an account meant to hold two.

The guard did not fail because its matching was wrong. It failed because
bot.process_utils.run_powershell returns "" on any error, which makes
list_processes() return [] and find_account_process() return None --
which is indistinguishable from "nobody else is running". A bot that
cannot verify it is alone must refuse to start, not assume the best.

This process is itself a python.exe, so an empty list can ONLY mean the
query failed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

SRC = Path("main.py").read_text()


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("duplicate-guard checks")

    # The guard must look at the raw process list, not only the
    # convenience wrapper that cannot distinguish empty from failed.
    check("main.py imports list_processes",
          re.search(r"from bot\.process_utils import [^\n]*list_processes", SRC) is not None)
    check("it enumerates python.exe before trusting the duplicate check",
          'list_processes("python.exe")' in SRC)

    guard = SRC[SRC.index('list_processes("python.exe")'):SRC.index("connector = MT5Connector")]
    check("an empty list is retried, not accepted", "attempt" in guard and "retry" in guard.lower())
    check("and after retries it EXITS rather than starting",
          "sys.exit(1)" in guard and "Refusing to" in guard)
    check("the exit happens BEFORE any MT5 connection is opened",
          SRC.index('list_processes("python.exe")') < SRC.index("connector = MT5Connector"))

    # The original check must still be there -- failing closed is in
    # addition to catching a real duplicate, not instead of it.
    check("a genuinely-found duplicate still exits",
          "Another main.py for account" in SRC and "Refusing to start a duplicate" in SRC)

    # And run_powershell's swallow-everything behaviour, the root cause,
    # must still be understood as returning "" so this guard stays needed.
    pu = Path("bot/process_utils.py").read_text()
    check("run_powershell still returns '' on failure (why the guard is needed)",
          'return ""' in pu)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
