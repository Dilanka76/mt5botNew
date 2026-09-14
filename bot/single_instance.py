"""One bot per account, guaranteed by the operating system.

WHY A MUTEX AND NOT A PROCESS SCAN. main.py's existing guard lists
running processes and exits if it sees another copy of itself. That has
two holes, and on 2026-09-08 both demo2 legs started a second copy that
each opened its own position -- four simultaneous trades on an account
meant to hold two:

  1. It FAILED OPEN. bot.process_utils.run_powershell returns "" on any
     error, so a timed-out query looked exactly like "nobody else is
     running". Fixed separately in main.py (commit 67da691).
  2. It cannot win a RACE. Two processes starting in the same second
     both scan, neither sees the other yet, and both proceed. The two
     Task Scheduler triggers that morning fired at 09:16:17 -- the same
     second.

A named mutex closes both. Creating it is atomic: exactly one process
can be the creator, however many ask at once, and Windows releases it
automatically when that process dies -- so there is no stale lock to
clean up after a crash, which a lock FILE would leave behind.

Not a substitute for the broker-side check in the engines' _enter():
this stops a second PROCESS, that stops a second POSITION. Real money
deserves both.

No-ops on non-Windows so the code stays importable on the dev Mac.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183


def acquire(account: str):
    """Claim sole ownership of `account`. Returns a handle to hold for the
    process's lifetime, or None if another process already holds it.

    Keep the returned handle alive -- letting it be garbage collected
    releases the mutex and lets a second bot in.
    """
    if not sys.platform.startswith("win"):
        logger.debug("single_instance: not Windows, skipping the mutex")
        return object()  # truthy sentinel; nothing to release

    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    # "Global\\" FIRST, session-local only as a fallback.
    #
    # This used to be session-local only, with a comment reasoning that
    # every bot runs as the same user so a session-local name is enough.
    # That reasoning has a hole: a task started by Task Scheduler runs in
    # Session 0 while one started from the app or a console runs in the
    # interactive session, and a session-local name lives in a SEPARATE
    # NAMESPACE per session. Two bots in two sessions would each create
    # "their" mutex successfully, see no conflict, and both trade -- the
    # exact double-position accident this file exists to prevent.
    #
    # A Global name is machine-wide and closes that. It needs
    # SeCreateGlobalPrivilege, which every bot here has (they all run as
    # Administrator), but if it is ever refused we fall back rather than
    # halt all trading -- and say loudly that the guard is now only as
    # wide as this session.
    for scope in ("Global\\", ""):
        name = f"{scope}mt5bot-main-{account}"
        handle = kernel32.CreateMutexW(None, True, name)
        last_error = ctypes.get_last_error()

        if handle and last_error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            # EXPECTED, not alarming: the main.py task carries a 5-minute
            # repeat trigger so a dead bot restarts within five minutes.
            # While one is healthy, every one of those attempts lands here
            # and exits. Logged at ERROR it wrote 288 alarms a day into
            # app.log and buried the real ones -- on 2026-09-14 a genuine
            # CRITICAL sat in that noise for hours.
            logger.info("single_instance: another main.py already holds %r -- exiting. "
                        "This is the normal outcome of the 5-minute restart trigger "
                        "while a healthy bot is running.", name)
            return None

        if handle:
            logger.info("single_instance: acquired %r", name)
            return handle

        if scope:
            logger.warning("single_instance: could not create the machine-wide lock %r "
                           "(error %s) -- falling back to a session-local one",
                           name, last_error)

    # Neither worked. Fail CLOSED -- an unverifiable guard is not a guard.
    # Same lesson as the process scan.
    logger.critical("single_instance: CreateMutexW failed (error %s) for %r -- "
                    "cannot verify this is the only instance", last_error, account)
    return None
