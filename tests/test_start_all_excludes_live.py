"""/start-all must not start real money, and must not clear a live kill switch.

    python3 tests/test_start_all_excludes_live.py

User's decision, 2026-09-14 (option A), after this happened for real: on
2026-09-13 one tap of the app's master toggle cleared live2_m3's and
live2_m5's kill switches and launched both live legs. The switches were
only noticed missing the next morning, hours before the account was due to
be funded.

The two directions are deliberately NOT symmetric:
  /stop-all   covers every account, live included -- worst case of an
              accidental tap is that trading halts
  /start-all  demo only -- real money is started one account at a time

Clearing the kill switch was the specific harm. It is how an operator
records "this account is deliberately stopped", and a master switch must
not be able to erase that.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "api_server.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def body(name: str) -> str:
    """Source of one route handler, up to the next def at column 0."""
    start = SRC.index(f"def {name}(")
    rest = SRC[start:]
    end = rest.find("\n@router.")
    return rest if end == -1 else rest[:end]


def main() -> None:
    start_all, stop_all = body("start_all"), body("stop_all")

    print("/start-all leaves real money alone")
    check("it branches on is_live", "if is_live:" in start_all)
    # The live branch must reach `continue` before any deactivate/launch.
    live_branch = start_all[start_all.index("if is_live:"):]
    live_branch = live_branch[:live_branch.index("continue")]
    check("the live branch never deactivates a kill switch",
          "deactivate" not in live_branch)
    check("the live branch never launches a process",
          "launch_python_script" not in live_branch)
    check("it reports the skip to the caller", '"skipped": True' in start_all)

    print("\nit still starts demo accounts")
    after = start_all[start_all.index("continue"):]
    check("a demo account still has its kill switch cleared",
          "kill_switch.deactivate()" in after)
    check("a demo account is still launched", "launch_python_script" in after)

    print("\n/stop-all still covers EVERYTHING, live included")
    check("stop-all has no is_live branch at all",
          not re.search(r"if\s+is_live\s*:", stop_all))
    check("stop-all still activates every kill switch",
          "kill_switch.activate(" in stop_all)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
