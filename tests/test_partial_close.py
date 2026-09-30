"""A scale-out must be a volume the broker will actually accept.

    python3 tests/test_partial_close.py

Added 2026-09-30 for the user's rule: close half the position a fixed
distance before its target and let the rest run. The dangerous part is not
the trigger, it is the VOLUME -- a broker refuses anything under its
minimum, anything that is not a whole number of steps, and anything that
would leave a remainder under the minimum. At 0.06 lots half is 0.03 and
both sides are legal; at 0.01 lots half is 0.005 and neither side is, so
the only honest answer is to leave the position alone rather than send an
order that will be rejected on every tick.

XAUUSDp: volume_min 0.01, volume_step 0.01.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for absent in ("MetaTrader5", "pandas", "yaml", "dotenv"):
    if absent not in sys.modules:
        try:
            __import__(absent)
        except ImportError:                       # not installed on a dev Mac
            stub = types.ModuleType(absent)
            stub.__getattr__ = lambda name: (lambda *a, **k: None)   # type: ignore[attr-defined]
            sys.modules[absent] = stub

from bot.execution.trade_executor import partial_volume       # noqa: E402

failures: list[str] = []
MIN, STEP = 0.01, 0.01


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


print("the real cases on this account")
check("0.06 lots halves to 0.03", partial_volume(0.06, 0.5, MIN, STEP) == 0.03)
check("0.05 rounds DOWN to 0.02, leaving 0.03", partial_volume(0.05, 0.5, MIN, STEP) == 0.02)
check("0.04 halves to 0.02", partial_volume(0.04, 0.5, MIN, STEP) == 0.02)
check("0.03 rounds down to 0.01, leaving 0.02", partial_volume(0.03, 0.5, MIN, STEP) == 0.01)
check("0.02 halves to 0.01, leaving 0.01", partial_volume(0.02, 0.5, MIN, STEP) == 0.01)

print("\nwhen a partial is impossible, it must refuse -- not send a bad order")
check("0.01 cannot be split at all", partial_volume(0.01, 0.5, MIN, STEP) is None)
check("a zero position is refused", partial_volume(0.0, 0.5, MIN, STEP) is None)
check("fraction 0 is refused", partial_volume(0.06, 0.0, MIN, STEP) is None)
check("fraction 1 is refused -- that is a full close, not a partial",
      partial_volume(0.06, 1.0, MIN, STEP) is None)
check("a zero step is refused", partial_volume(0.06, 0.5, MIN, 0.0) is None)

print("\nthe remainder must also be tradeable")
# 0.03 at 0.8 would close 0.024 -> rounds to 0.02, remainder 0.01: legal
check("0.03 at 80% closes 0.02 and leaves 0.01", partial_volume(0.03, 0.8, MIN, STEP) == 0.02)
# 0.02 at 0.9 would close 0.018 -> rounds to 0.01, remainder 0.01: legal
check("0.02 at 90% closes 0.01 and leaves 0.01", partial_volume(0.02, 0.9, MIN, STEP) == 0.01)
# 0.01 at 0.9 -> 0.009 -> rounds to 0.00: refused
check("0.01 at 90% is refused", partial_volume(0.01, 0.9, MIN, STEP) is None)

print("\nit never closes MORE than asked, and never all of it")
for total in (0.02, 0.03, 0.05, 0.06, 0.11, 0.57, 1.23):
    part = partial_volume(total, 0.5, MIN, STEP)
    if part is None:
        continue
    check(f"{total:.2f} -> {part:.2f}: not more than half, and something is left",
          part <= total * 0.5 + 1e-9 and part < total and round(total - part, 8) >= MIN)

print("\na broker with a bigger minimum")
check("0.06 with a 0.10 minimum is refused", partial_volume(0.06, 0.5, 0.10, 0.01) is None)
check("1.00 with a 0.10 minimum and 0.10 step closes 0.50",
      partial_volume(1.00, 0.5, 0.10, 0.10) == 0.50)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
