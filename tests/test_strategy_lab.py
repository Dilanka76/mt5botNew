"""The lab must trade its own rules, to the cent, and never see the future.

    python3 tests/test_strategy_lab.py

scripts/strategy_lab.py decides which strategy family, if any, gets built
after the EMA13/21 family was proven dead. Two hand-written tests have
already caught real bugs today -- a sign error in a SELL exit and a loop
that dropped the final candle's exit -- so this one checks the shared
simulator by hand before any result is read from it.
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
    "strategy_lab", Path(__file__).resolve().parents[1] / "scripts" / "strategy_lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)                      # type: ignore[union-attr]

failures: list[str] = []
T0 = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
COSTS = lab.COSTS_PER_OZ
HOLD = timedelta(hours=72)


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Column(list):
    def tolist(self):
        return list(self)


class Frame:
    def __init__(self, candles):
        self.index = [T0 + timedelta(minutes=15 * i) for i in range(len(candles))]
        keys = ("open", "high", "low", "close", "rsi")
        self._cols = {k: Column(c[k] for c in candles) for k in keys}

    def __getitem__(self, name):
        return self._cols[name]

    def __len__(self):
        return len(self.index)


def bar(o=100.0, high=None, low=None, close=None, rsi=50.0):
    return {"open": o, "high": high if high is not None else o + 0.2,
            "low": low if low is not None else o - 0.2,
            "close": close if close is not None else o, "rsi": rsi}


print("a BUY that reaches its target")
# signal on candle 0, entry at candle 1's OPEN (100), stop 5 below, target 10 above
sig = [(1, 5.0, 10.0), None, None, None]
t = lab.simulate(Frame([bar(), bar(100.0), bar(100, high=100.5),
                        bar(100, high=110.5, low=99.9)]), sig, "bollinger", HOLD)
check("one trade", len(t) == 1)
check("closed by the target", t and t[0]["reason"] == "target")
check(f"it earns the target minus costs ({10 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (10 - COSTS)) < 1e-9)

print("\nthe stop is taken when price reaches it")
t = lab.simulate(Frame([bar(), bar(100.0), bar(100, high=100.3, low=94.5)]),
                 [(1, 5.0, 10.0), None, None], "bollinger", HOLD)
check("closed by the stop", t and t[0]["reason"] == "stop")
check(f"losing the stop distance plus costs ({-5 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (-5 - COSTS)) < 1e-9)

print("\none candle touching BOTH is scored as the stop")
t = lab.simulate(Frame([bar(), bar(100.0), bar(100, high=111.0, low=94.0)]),
                 [(1, 5.0, 10.0), None, None], "bollinger", HOLD)
check("scored as the stop, the harsh way", t and t[0]["reason"] == "stop")

print("\na SELL earns when price falls")
t = lab.simulate(Frame([bar(), bar(100.0), bar(100, high=100.2, low=89.5)]),
                 [(-1, 5.0, 10.0), None, None], "bollinger", HOLD)
check("it is a SELL", t and t[0]["direction"] == "SELL")
check(f"and earns its target minus costs ({10 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (10 - COSTS)) < 1e-9)

print("\nno second trade while one is open")
sig = [(1, 5.0, 10.0), (1, 5.0, 10.0), (1, 5.0, 10.0), None, None]
t = lab.simulate(Frame([bar(), bar(100.0), bar(100.0), bar(100.0),
                        bar(100, high=110.5)]), sig, "bollinger", HOLD)
check("only one trade opened", len(t) == 1)

print("\ndonchian turns on the opposite signal, with no target")
sig = [(1, 5.0, None), None, (-1, 5.0, None), None]
t = lab.simulate(Frame([bar(), bar(100.0), bar(100, close=103.0), bar(100.0)]),
                 sig, "donchian", HOLD)
check("closed by the opposite signal", t and t[0]["reason"] == "signal")
check(f"at that candle's close ({3 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (3 - COSTS)) < 1e-9)

print("\nrsi closes when the market is no longer stretched")
sig = [(1, 5.0, None), None, None]
t = lab.simulate(Frame([bar(rsi=25), bar(100.0, rsi=30), bar(100, close=102.0, rsi=55)]),
                 sig, "rsi", HOLD)
check("closed by the RSI signal", t and t[0]["reason"] == "signal")
check(f"at that candle's close ({2 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (2 - COSTS)) < 1e-9)

print("\nnothing opens on the very last candle -- there is no next open")
t = lab.simulate(Frame([bar(), bar(100.0)]), [None, (1, 5.0, 10.0)], "bollinger", HOLD)
check("no trade", t == [])

print("\nthe max hold closes the trade at that candle's price")
bars = [bar(), bar(100.0)] + [bar(100, high=100.3, low=99.7, close=101.5) for _ in range(400)]
sigs = [(1, 5.0, 10.0)] + [None] * (len(bars) - 1)
t = lab.simulate(Frame(bars), sigs, "bollinger", HOLD)
check("closed by max hold", t and t[0]["reason"] == "max hold")
check(f"at that candle's close ({1.5 - COSTS:+.2f})",
      t and abs(t[0]["oz"] - (1.5 - COSTS)) < 1e-9)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
