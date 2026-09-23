"""The size arithmetic must be arithmetic -- every number checked by hand.

    python3 tests/test_size_arithmetic.py

scripts/size_arithmetic.py re-prices real trades at different lot sizes
so the account owner can choose a size with numbers in front of him. If
it is wrong, it is worse than useless, so every rule it relies on is
pinned here: when a trade's size is decided, when its money lands, how
lots round, what a wipeout stops, and how the worst drop is measured.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mt5 = types.ModuleType("MetaTrader5")
mt5.__getattr__ = lambda name: 0                     # type: ignore[attr-defined]
sys.modules["MetaTrader5"] = mt5
for absent in ("pandas", "yaml", "dotenv"):
    if absent not in sys.modules:
        try:
            __import__(absent)
        except ImportError:                          # not installed on a dev Mac
            stub = types.ModuleType(absent)
            stub.__getattr__ = lambda name: (lambda *a, **k: None)   # type: ignore[attr-defined]
            sys.modules[absent] = stub

import importlib.util                                 # noqa: E402

spec = importlib.util.spec_from_file_location(
    "size_arithmetic", Path(__file__).resolve().parent.parent / "scripts" / "size_arithmetic.py")
sizing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sizing)                       # type: ignore[union-attr]

failures: list[str] = []
T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
LEG = "demo2_m3"


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def tier(max_balance, lots):
    return NS(max_balance=max_balance, lots=lots)


def trade(oz, open_min, close_min, account=LEG):
    return {"account": account, "oz": oz,
            "entry_time": T0 + timedelta(minutes=open_min),
            "exit_time": T0 + timedelta(minutes=close_min)}


def go(rule, trades, ladder, start, smooth_step=75.0):
    accounts = {t["account"] for t in trades}
    return sizing.run(rule, trades, {a: ladder for a in accounts},
                      {a: smooth_step for a in accounts}, start)


flat = [tier(None, 0.02)]

print("one trade, by hand")
r = go(sizing.ladder_rule(1.0), [trade(+1.0, 0, 10)], flat, 300.0)
check("0.02 lots on +1.00 $/oz pays $2.00", abs(r["balance"] - 302.0) < 1e-9)
r = go(sizing.ladder_rule(0.5), [trade(+1.0, 0, 10)], flat, 300.0)
check("half the ladder pays half", abs(r["balance"] - 301.0) < 1e-9)
r = go(sizing.flat_minimum, [trade(-4.0, 0, 10)], flat, 300.0)
check("the minimum 0.01 loses $4.00 on -4.00 $/oz", abs(r["balance"] - 296.0) < 1e-9)

print("\nlots round DOWN to a whole 0.01, and never below the minimum")
check("0.06 scaled by a third is 0.02", sizing.down_to_step(0.06 / 3) == 0.02)
check("0.03 scaled by a third is 0.01", sizing.down_to_step(0.03 / 3) == 0.01)
check("0.03 halved rounds DOWN to 0.01", sizing.down_to_step(0.015) == 0.01)
check("a quarter of 0.02 is still 0.01, not zero", sizing.down_to_step(0.005) == 0.01)
check("the smooth rule at $300 with $75 a step gives 0.04",
      sizing.smooth_rule(300.0, flat, 75.0) == 0.04)
check("the smooth rule at $150 gives 0.02", sizing.smooth_rule(150.0, flat, 75.0) == 0.02)

print("\nsize is decided when the trade OPENS, from the balance right then")
steps = [tier(100, 0.02), tier(None, 0.04)]
r = go(sizing.ladder_rule(1.0), [trade(+1.0, 0, 10), trade(+1.0, 10, 20)], steps, 100.0)
check("a closed winner lifts the NEXT trade's tier (100 -> 102 -> 106)",
      abs(r["balance"] - 106.0) < 1e-9)
r = go(sizing.ladder_rule(1.0), [trade(+1.0, 0, 10), trade(+1.0, 5, 20)], steps, 100.0)
check("a trade opened BEFORE that win keeps the old tier (100 -> 104)",
      abs(r["balance"] - 104.0) < 1e-9)

print("\nboth legs share one balance")
two = [trade(+1.0, 0, 10, "demo2_m3"), trade(+1.0, 10, 20, "demo2_m5")]
r = go(sizing.ladder_rule(1.0), two, steps, 100.0)
check("the M5 leg sizes off the balance the M3 leg left behind",
      abs(r["balance"] - 106.0) < 1e-9)

print("\na wipeout stops the account")
r = go(sizing.ladder_rule(1.0), [trade(-10.0, 0, 10), trade(+50.0, 20, 30)],
       [tier(None, 0.04)], 10.0)
check("balance below zero is a wipeout", r["wiped_at"] is not None)
check("no trade opens after it", r["taken"] == 1)
check("the miracle winner afterwards is not counted", r["balance"] <= 0)

print("\nthe worst drop is measured peak to trough")
r = go(sizing.ladder_rule(1.0), [trade(+10.0, 0, 10), trade(-10.0, 20, 30)], flat, 100.0)
check("100 -> 120 -> 100 is a $20 drop", abs(r["worst_dd"] - 20.0) < 1e-9)
check("and 17% of the peak", abs(r["worst_dd_pct"] - 100 * 20 / 120) < 1e-6)
check("the lowest balance is the start here", abs(r["low"] - 100.0) < 1e-9)

print("\nan open trade still closes after a wipeout, and is not double counted")
r = go(sizing.ladder_rule(1.0), [trade(-10.0, 0, 30), trade(-1.0, 10, 20)],
       [tier(None, 0.04)], 10.0)
check("the trade already open when it happened still settles", r["taken"] == 2)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
