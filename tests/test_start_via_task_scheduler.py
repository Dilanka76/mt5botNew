"""The app's Start must launch a bot through its scheduled task, so the bot
belongs to Windows and not to the gateway.

    python3 tests/test_start_via_task_scheduler.py

REAL INCIDENT, 2026-09-22. The gateway had been running since 09-15 --
older than the code that lets the app start live -- so it was restarted.
That killed demo2_m3/m5: bots launched by the gateway are its CHILDREN
and die with it. A gateway restart, hang or crash must never be able to
take a live bot down in the middle of a trade.

Also: a DISABLED task means the account was switched off on purpose (every
retired M1 leg), so the app must refuse, and must not clear its kill
switch on the way to refusing.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

m = types.ModuleType("MetaTrader5")
m.__getattr__ = lambda name: 0                     # type: ignore[attr-defined]
sys.modules.setdefault("MetaTrader5", m)

import bot.process_utils as pu                      # noqa: E402
import api_server                                   # noqa: E402
from fastapi import HTTPException                   # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class FakeSwitch:
    def __init__(self, active):
        self.active, self.cleared = active, False

    def is_active(self):
        return self.active

    def deactivate(self):
        self.active, self.cleared = False, True


def main() -> None:
    print("reading Task Scheduler")
    pu.os = NS(name="nt")
    replies = {}
    pu.subprocess = NS(run=lambda args, **k: replies[tuple(args[:2])])
    replies[("schtasks", "/Query")] = NS(returncode=0, stdout='"\\MT5-Bot-live2_m3","N/A","Ready"\n', stderr="")
    check("a Ready task reads as Ready", pu.scheduled_task_state("MT5-Bot-live2_m3") == "Ready")
    replies[("schtasks", "/Query")] = NS(returncode=0, stdout='"\\MT5-Bot-live2_m1","N/A","Disabled"\n', stderr="")
    check("a disabled task reads as Disabled", pu.scheduled_task_state("MT5-Bot-live2_m1") == "Disabled")
    replies[("schtasks", "/Query")] = NS(returncode=1, stdout="", stderr="ERROR: cannot find the file")
    check("no such task -> None", pu.scheduled_task_state("MT5-Bot-nope") is None)
    replies[("schtasks", "/Run")] = NS(returncode=0, stdout="SUCCESS", stderr="")
    check("a started task -> True", pu.run_scheduled_task("MT5-Bot-live2_m3") is True)

    print("\nthe gateway's start")
    launched = []
    api_server.launch_python_script = lambda *a, **k: launched.append(a) or 4242
    api_server.find_account_process = lambda *a, **k: None
    ran = []
    api_server.run_scheduled_task = lambda task: ran.append(task) or True
    switches = {}
    api_server.app.state.kill_switches = switches

    api_server.scheduled_task_state = lambda task: "Ready"
    switches["live2_m3"] = FakeSwitch(active=True)
    r = api_server.start(config=NS(account="live2_m3"))
    check("an account WITH a task is started through the task", ran == ["MT5-Bot-live2_m3"])
    check("...and NOT as the gateway's own child", launched == [])
    check("...and says so", r["launched_via"] == "task_scheduler")
    check("its kill switch is cleared, as before", switches["live2_m3"].cleared)

    ran.clear()
    api_server.scheduled_task_state = lambda task: None
    switches["demo9_m3"] = FakeSwitch(active=False)
    r = api_server.start(config=NS(account="demo9_m3"))
    check("an account with NO task still starts (the old direct way)",
          launched and r["launched_via"] == "gateway" and ran == [])

    launched.clear()
    api_server.scheduled_task_state = lambda task: "Disabled"
    switches["live2_m1"] = FakeSwitch(active=True)
    refused = False
    try:
        api_server.start(config=NS(account="live2_m1"))
    except HTTPException as exc:
        refused = exc.status_code == 409
    check("a DISABLED task (a retired account) is refused", refused)
    check("...its kill switch is left ON", switches["live2_m1"].active and not switches["live2_m1"].cleared)
    check("...and nothing is launched", launched == [])

    api_server.scheduled_task_state = lambda task: "Ready"
    api_server.run_scheduled_task = lambda task: False
    switches["live2_m5"] = FakeSwitch(active=True)
    r = api_server.start(config=NS(account="live2_m5"))
    check("if the task will not start, it falls back rather than leaving the account down",
          r["launched_via"] == "gateway" and launched)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
