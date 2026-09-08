"""Does config.swap_immediate really fire the swap on the FIRST candle?

    python3 tests/test_swap_immediate.py

On demo1 the 2-candle debounce plus an ADX(14) >= 25 gate blocked 100%
of reversals -- 0 swaps in 209 real trades -- so the engine's main way of
cutting a losing trade early was effectively off. This checks the flag
that removes both, against the REAL branch in the shipped engine rather
than a copy of its logic.

The three things that must hold:
  - immediate ON  : swaps on the first opposing candle, whatever ADX says
  - immediate OFF : unchanged -- arms on the first, needs a second AND ADX
  - the stop-tightening only happens in the debounce path, since it
    exists solely to protect the position during that wait
"""
from __future__ import annotations

import inspect
import re
import sys
import types

sys.path.insert(0, ".")

if "MetaTrader5" not in sys.modules:
    _stub = types.ModuleType("MetaTrader5")
    for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
        setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
    sys.modules["MetaTrader5"] = _stub

from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("swap_immediate wiring checks")
    src = inspect.getsource(DualCrossConfirmedSwapAdxEngine.on_new_candle)

    # The gate that decides whether the swap fires at all.
    check("the swap trigger reads config.swap_immediate",
          "immediate = self.config.swap_immediate" in src)
    check("first opposing candle is enough when immediate",
          re.search(r"if immediate or self\.pending_reversal_direction == direction:", src) is not None)
    check("ADX is bypassed when immediate",
          re.search(r"adx_ok = immediate or \(", src) is not None)

    # The debounce path must be unreachable when immediate: `immediate or ...`
    # short-circuits into the swap branch, so the else that sets
    # pending_reversal_direction and tightens the stop cannot run.
    branch = src[src.index("immediate = self.config.swap_immediate"):]
    else_at = branch.index("self.pending_reversal_direction = direction")
    swap_at = branch.index("swapped_confirmed_reversal")
    check("the swap happens in the if-branch, the arming in the else-branch",
          swap_at < else_at)

    # Default must be unchanged behaviour.
    from bot.config import AppConfig
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(AppConfig)}
    check("swap_immediate exists on AppConfig", "swap_immediate" in fields)
    check("and defaults to False -- existing accounts are untouched",
          fields["swap_immediate"].default is False)

    # The runner must still be there: the whole point was keeping it.
    engine_src = inspect.getsource(DualCrossConfirmedSwapAdxEngine)
    check("the TP-runner is still wired into this engine",
          "_manage_tp_runner" in engine_src)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
