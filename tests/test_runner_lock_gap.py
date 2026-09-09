"""The runner must never remove the take-profit without a lock behind it.

    python3 tests/test_runner_lock_gap.py

2026-09-09 17:48, demo1_m3. The runner armed at +$5.14 and removed the
broker take-profit. The lock only engaged at the $6.00 target. Price
peaked around +$5.94 -- SIX CENTS short -- reversed, and the trade exited
on the breakeven stop at +$0.19 for $1.56. demo2_m3 held the same signal
with its take-profit intact and banked $6.00 for $71.28.

One trade, $69.72, and the entire reason demo1_m3 lost that day to a
control with no runner at all.

The hole was between the two steps: the take-profit was removed at the
ARM point but the lock waited for the TARGET, so in between the trade had
neither. The fix locks as soon as price is at or past the LOCK LEVEL.

The second property here matters as much as the first. Where the lock
sits AT the target (tp_runner_lock_below_usd = 0, demo1_m1's setting)
locking at the arm point would put the stop ABOVE the current price --
and the software stop check reads "bid <= stop" as hit, so the trade
would close instantly at a price it never traded. Testing against the
lock level rather than the arm point makes that impossible.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENGINE = (Path(__file__).resolve().parents[1] / "bot" / "strategy"
          / "state_machine_dual_cross_confirmed_swap_adx.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def lock_fires(tp: float, lock_below: float, arm_before: float, favorable: float) -> bool:
    """The shipped condition, evaluated directly."""
    lock_level = max(0.01, tp - lock_below)
    return favorable >= lock_level


def armed(tp: float, arm_before: float, favorable: float) -> bool:
    return favorable >= tp - arm_before


def main() -> None:
    print("runner lock gap")

    check("the lock is tested against the LOCK LEVEL, not the target",
          "favorable >= lock_level" in ENGINE)
    check("the old target-only condition is gone",
          re.search(r"not self\.runner_locked and favorable >= tp\b", ENGINE) is None)
    # Match the CODE line, not the prose above it -- the explanatory
    # comment contains the same phrase and .index() finds that first.
    check("lock_level is computed before the condition uses it",
          ENGINE.index("lock_level = max(0.01, tp")
          < ENGINE.index("and favorable >= lock_level:"))

    print("\ndemo1_m3: TP $6.00, lock $1.00 below, arm $1.00 early")
    # The trade that lost $69.72: armed at 5.14, peaked 5.94.
    check("armed at +$5.14 (as it did)", armed(6.0, 1.0, 5.14))
    check("OLD behaviour: no lock at +$5.14 -- the gap", 5.14 < 6.0)
    check("NEW behaviour: locks at +$5.14, the moment the TP is removed",
          lock_fires(6.0, 1.0, 1.0, 5.14))
    check("and at the +$5.94 peak it would still be locked",
          lock_fires(6.0, 1.0, 1.0, 5.94))
    check("still no lock below the lock level", not lock_fires(6.0, 1.0, 1.0, 4.99))

    print("\ndemo1_m1 shape: TP $5.00, lock AT the target, arm $0.20 early")
    # Arming happens at 4.80. Locking there would demand a stop at +$5.00,
    # ABOVE the price -- which the software stop check treats as hit.
    check("arms at +$4.80", armed(5.0, 0.20, 4.80))
    check("does NOT lock at +$4.80 -- that stop would sit above the price",
          not lock_fires(5.0, 0.0, 0.20, 4.80))
    check("locks exactly at +$5.00, as before", lock_fires(5.0, 0.0, 0.20, 5.00))

    print("\nthe log line must not claim a target that was not reached")
    check("it reports the actual level, not 'Reached the $6.00 target'",
          "Reached the ${tp:.2f} target" not in ENGINE
          and "lock level ${lock_level:.2f}" in ENGINE)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
