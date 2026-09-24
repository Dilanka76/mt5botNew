"""The range strategy must trade the way the rule says, to the cent.

    python3 tests/test_range_strategy.py

scripts/range_strategy_test.py decides whether this project builds a
second strategy after the first one was proven dead, so its simulation
gets checked by hand before anyone reads a result from it. A sign error
in the SELL exit was caught this way and fixed before the first run.

Every case below is worked out on paper: ceiling 100, floor 90, so the
height is 10, the target is the far side and the stop sits 2.5 beyond
the level. Costs are 0.18 $/oz a trade.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for absent in ("pandas", "yaml", "dotenv", "MetaTrader5"):
    if absent not in sys.modules:
        try:
            __import__(absent)
        except ImportError:                       # not installed on a dev Mac
            stub = types.ModuleType(absent)
            stub.__getattr__ = lambda name: (lambda *a, **k: None)   # type: ignore[attr-defined]
            sys.modules[absent] = stub

spec = importlib.util.spec_from_file_location(
    "range_strategy_test",
    Path(__file__).resolve().parents[1] / "scripts" / "range_strategy_test.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)                       # type: ignore[union-attr]

failures: list[str] = []
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
CEILING, FLOOR = 100.0, 90.0
HEIGHT = CEILING - FLOOR
COSTS = mod.COSTS_PER_OZ


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Column(list):
    def tolist(self):
        return list(self)


class Frame:
    """The handful of DataFrame behaviours simulate() actually uses."""

    def __init__(self, candles):
        self.index = [T0 + timedelta(minutes=3 * i) for i in range(len(candles))]
        self._cols = {name: Column(c[k] for c in candles)
                      for name, k in (("high", "high"), ("low", "low"), ("close", "close"),
                                      ("range_state", "state"), ("range_ceiling", "ceiling"),
                                      ("range_floor", "floor"))}

    def __getitem__(self, name):
        return self._cols[name]

    def __len__(self):
        return len(self.index)


def bar(high, low, close=None, state=1.0, ceiling=CEILING, floor=FLOOR):
    return {"high": high, "low": low, "close": close if close is not None else (high + low) / 2,
            "state": state, "ceiling": ceiling, "floor": floor}


print("a SELL at the ceiling that reaches the floor")
t = mod.simulate(Frame([bar(95, 93), bar(101, 99), bar(96, 89.5), bar(95, 94)]))
check("one trade", len(t) == 1)
check("it is a SELL", t and t[0]["direction"] == "SELL")
check("closed by the target", t and t[0]["reason"] == "target")
check(f"it earns the range height minus costs ({HEIGHT - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (HEIGHT - COSTS)) < 1e-9)

print("\na SELL that breaks out and is stopped")
t = mod.simulate(Frame([bar(95, 93), bar(101, 99), bar(103, 101), bar(104, 102)]))
check("closed by the stop", t and t[0]["reason"] == "stop")
check(f"it loses a quarter of the height plus costs ({-(0.25 * HEIGHT) - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (-(0.25 * HEIGHT) - COSTS)) < 1e-9)

print("\none candle touching BOTH the stop and the target is scored as the stop")
t = mod.simulate(Frame([bar(95, 93), bar(101, 99), bar(103, 89)]))
check("scored as the stop, the harsh way", t and t[0]["reason"] == "stop")
check("so it is a loss", t and t[0]["oz"] < 0)

print("\nafter a stop-out, no new trade until price is back INSIDE the range")
t = mod.simulate(Frame([
    bar(95, 93), bar(101, 99), bar(104, 102),      # sell, then stopped
    bar(105, 103), bar(106, 104),                  # still outside: must NOT re-enter
    bar(101, 99, close=95),                        # closes back inside: re-armed
    bar(102, 100), bar(93, 89.5),                  # a fresh sell, reaching the floor
]))
check("exactly two trades, not four", len(t) == 2)
check("the first was the stop", t and t[0]["reason"] == "stop")
check("the second was allowed only after price came back inside",
      len(t) > 1 and t[1]["opened"] >= T0 + timedelta(minutes=3 * 6))

print("\nboth levels touched in one candle is ambiguous, so no trade")
t = mod.simulate(Frame([bar(95, 93), bar(101, 89)]))
check("no trade at all", t == [])

print("\nno range, no trade")
t = mod.simulate(Frame([bar(101, 99, state=0.0), bar(102, 100, state=0.0)]))
check("state 0 is ignored", t == [])

print("\nunresolved after 48 hours, closed at the candle's price")
bars = [bar(95, 93), bar(101, 99)] + [bar(99, 96, close=97) for _ in range(1000)]
t = mod.simulate(Frame(bars))
check("closed by time", t and t[0]["reason"] == "time")
check("at the candle's close, minus costs",
      t and abs(t[0]["oz"] - ((CEILING - 97.0) - COSTS)) < 1e-9)

print("\na BUY at the floor that reaches the ceiling")
t = mod.simulate(Frame([bar(95, 93), bar(91, 89), bar(101, 92)]))
check("it is a BUY", t and t[0]["direction"] == "BUY")
check(f"it earns the height minus costs ({HEIGHT - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (HEIGHT - COSTS)) < 1e-9)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
