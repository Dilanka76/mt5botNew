"""The hold simulation must trade the way the rule says, to the cent.

    python3 tests/test_hold_strategy.py

scripts/hold_strategy_year.py is Stage 1 of deciding whether this project
builds a second strategy, so its loop is checked by hand first. The
earlier range simulator had a sign error in its SELL exit that a test
like this caught before anyone read a result.

Worked on paper: entry at 100, target $6 against the M15 trend and $8
with it, backstop $30, costs 0.18 $/oz.
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
        except ImportError:
            stub = types.ModuleType(absent)
            stub.__getattr__ = lambda name: (lambda *a, **k: None)   # type: ignore[attr-defined]
            sys.modules[absent] = stub

spec = importlib.util.spec_from_file_location(
    "hold_strategy_year",
    Path(__file__).resolve().parents[1] / "scripts" / "hold_strategy_year.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)                     # type: ignore[union-attr]

failures: list[str] = []
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
COSTS = mod.COSTS_PER_OZ
HOLD = timedelta(hours=48)


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Column(list):
    def tolist(self):
        return list(self)


class Frame:
    def __init__(self, candles):
        self.index = [T0 + timedelta(minutes=3 * i) for i in range(len(candles))]
        keys = ("open", "high", "low", "close", "ema13", "ema21", "htf_trend")
        self._cols = {k: Column(c[k] for c in candles) for k in keys}

    def __getitem__(self, name):
        return self._cols[name]

    def __len__(self):
        return len(self.index)


def bar(o=100.0, *, e13=101.0, e21=100.0, high=None, low=None, close=None, trend=1.0):
    """Defaults keep EMA13 above EMA21 with no fresh cross, so a candle is
    inert unless the test explicitly makes it cross."""
    return {"open": o, "high": high if high is not None else o + 0.2,
            "low": low if low is not None else o - 0.2,
            "close": close if close is not None else o,
            "ema13": e13, "ema21": e21, "htf_trend": trend}


print("a BUY that reaches its target")
# candle 1 confirms the up-cross; entry is candle 2's OPEN (100.0)
t = mod.simulate(Frame([
    bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0), bar(100, high=108.5, low=99.8),
]), 6.0, 8.0, 30.0, HOLD)
check("one trade", len(t) == 1)
check("it is a BUY", t and t[0]["direction"] == "BUY")
check("closed by the target", t and t[0]["reason"] == "target")
check(f"it earns the $8 trend target minus costs ({8 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (8 - COSTS)) < 1e-9)

print("\nagainst the M15 trend the target is the smaller one")
t = mod.simulate(Frame([
    bar(e13=99, e21=100, trend=-1.0), bar(e13=101, e21=100, trend=-1.0), bar(100.0, trend=-1.0),
    bar(100, high=106.5, low=99.8, trend=-1.0),
]), 6.0, 8.0, 30.0, HOLD)
check(f"it earns $6 minus costs ({6 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (6 - COSTS)) < 1e-9)

print("\nthe opposite cross is IGNORED -- that is the whole point")
t = mod.simulate(Frame([
    bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0),
    bar(100, high=100.5, low=99.5), bar(e13=99, e21=100, high=100.2, low=99.0),   # cross back down
    bar(100, high=100.4, low=99.6), bar(100, high=108.5, low=99.9),       # target later
]), 6.0, 8.0, 30.0, HOLD)
check("still only one trade, and it won", len(t) == 1 and t[0]["reason"] == "target")

print("\nthe backstop still works")
t = mod.simulate(Frame([
    bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0), bar(100, high=100.2, low=69.0),
]), 6.0, 8.0, 30.0, HOLD)
check("closed by the backstop", t and t[0]["reason"] == "backstop")
check(f"losing $30 plus costs ({-30 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (-30 - COSTS)) < 1e-9)

print("\none candle touching BOTH counts as the backstop")
t = mod.simulate(Frame([
    bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0), bar(100, high=110.0, low=69.0),
]), 6.0, 8.0, 30.0, HOLD)
check("scored as the backstop, the harsh way", t and t[0]["reason"] == "backstop")

print("\nno second trade while one is open")
bars = [bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0)]
bars += [bar(e13=99, e21=100, high=100.3, low=99.7), bar(e13=101, e21=100, high=100.3, low=99.7)] * 5
bars += [bar(100, high=108.5, low=99.9)]
t = mod.simulate(Frame(bars), 6.0, 8.0, 30.0, HOLD)
check("five more crosses fired and were all ignored", len(t) == 1)

print("\na SELL earns when price falls")
t = mod.simulate(Frame([
    bar(e13=101, e21=100), bar(e13=99, e21=100), bar(100.0), bar(100, high=100.2, low=93.5),
]), 6.0, 8.0, 30.0, HOLD)
check("it is a SELL", t and t[0]["direction"] == "SELL")
check(f"and earns its target minus costs ({6 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (6 - COSTS)) < 1e-9)

print("\nunresolved after the max hold, closed at that candle's close")
bars = [bar(e13=99, e21=100), bar(e13=101, e21=100), bar(100.0)]
bars += [bar(100, high=100.4, low=99.6, close=100.9) for _ in range(2000)]
t = mod.simulate(Frame(bars), 6.0, 8.0, 30.0, HOLD)
check("closed at 48h", t and t[0]["reason"] == "48h")
check("at that candle's close, minus costs",
      t and abs(t[0]["oz"] - (0.9 - COSTS)) < 1e-9)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
