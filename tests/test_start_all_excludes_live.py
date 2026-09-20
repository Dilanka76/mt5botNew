"""/start-all must not start real money, and must not clear a live kill switch.

    python3 tests/test_start_all_excludes_live.py

User's decision, 2026-09-14 (option A), after this happened for real: on
2026-09-13 one tap of the app's master toggle cleared live2_m3's and
live2_m5's kill switches and launched both live legs. The switches were
only noticed missing the next morning, hours before the account was due to
be funded.

The two directions are deliberately NOT symmetric:
  /stop-all   defaults to every account, live included -- worst case of an
              accidental tap is that trading halts
  /start-all  defaults to demo only -- real money is started deliberately

Since 2026-09-15 both take a `scope` ("all" / "live" / "demo") so the app
can offer two master switches. The defaults above are unchanged, and an
account outside the named scope must be left completely untouched.

Clearing the kill switch was the specific harm. It is how an operator
records "this account is deliberately stopped", and a master switch must
not be able to erase that.
"""
from __future__ import annotations

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
    in_scope = body("_in_scope")

    print("the master toggles name the group they mean")
    # Rewritten 2026-09-20: the original checked for a literal "if is_live:"
    # in start_all, which 5ec9877 replaced with the shared _in_scope()
    # helper when the app gained separate LIVE and DEMO toggles. The rule
    # being protected is unchanged -- a bulk call must never touch an
    # account outside the group it was asked for, and start-all must still
    # default to demo.
    check("start-all defaults to demo", 'def start_all(scope: str = "demo")' in start_all)
    check("stop-all defaults to everything", 'def stop_all(scope: str = "all")' in stop_all)
    for name, src in (("start-all", start_all), ("stop-all", stop_all)):
        check(f"{name} asks _in_scope before acting", "_in_scope(is_live, scope)" in src)
        gate = src[src.index("_in_scope(is_live, scope)"):]
        gate = gate[:gate.index("continue")]
        check(f"{name} does nothing to an out-of-scope account",
              "deactivate" not in gate and "activate(" not in gate
              and "launch_python_script" not in gate)

    print("\n_in_scope itself")
    check("scope 'live' selects only live", 'if scope == "live":\n        return is_live' in in_scope)
    check("anything unrecognised means demo, never live", "return not is_live" in in_scope)

    print("\nit still does the work for in-scope accounts")
    after = start_all[start_all.index("continue"):]
    check("an in-scope account has its kill switch cleared",
          "kill_switch.deactivate()" in after)
    check("an in-scope account is still launched", "launch_python_script" in after)
    check("it reports what it skipped", '"skipped": True' in start_all)
    check("stop-all still activates kill switches", "kill_switch.activate(" in stop_all)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
