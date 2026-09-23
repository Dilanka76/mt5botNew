"""A flip chain must be exactly what the engine really does.

    python3 tests/test_flip_chain.py

scripts/flip_chain_test.py decides which trades belong to one chain of
reversals. Get that wrong and the whole study is wrong, so the rules are
pinned here against the real live2_m3 sequence of 2026-09-22, read from
the audit output: a fresh SELL, two reversals off it, then a separate
trade after a take-profit -- which must NOT join the chain, because the
bot really was flat in between.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mt5 = types.ModuleType("MetaTrader5")
mt5.__getattr__ = lambda name: 0                     # type: ignore[attr-defined]
sys.modules["MetaTrader5"] = mt5
for absent in ("pandas", "yaml", "dotenv"):
    if absent not in sys.modules:
        try:
            __import__(absent)
        except ImportError:                             # not installed on a dev Mac
            stub = types.ModuleType(absent)
            stub.__getattr__ = lambda name: (lambda *a, **k: None)   # type: ignore[attr-defined]
            sys.modules[absent] = stub

import importlib.util                                  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "flip_chain_test", Path(__file__).resolve().parent.parent / "scripts" / "flip_chain_test.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)                        # type: ignore[union-attr]
build_chains, SWAP = module.build_chains, module.SWAP_EXIT

failures: list[str] = []
T = lambda h, m: datetime(2026, 9, 22, h, m, tzinfo=timezone.utc)   # noqa: E731


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def trade(direction, enter, exit_at, reason, profit=0.0, volume=0.04):
    return {"direction": direction, "entry_time": enter, "exit_time": exit_at,
            "exit_reason": reason, "profit": profit, "volume": volume}


print("the real live2_m3 chain, 2026-09-22 05:21-05:57")
rows = build_chains([
    trade("SELL", T(5, 21), T(5, 33), SWAP, -57.06, 0.06),
    trade("BUY", T(5, 33), T(5, 57), SWAP, -53.34, 0.06),
    trade("SELL", T(5, 57), T(6, 31), "Take Profit", +23.88, 0.04),
    trade("BUY", T(9, 6), T(9, 42), "Take Profit", +18.18, 0.03),
])
check("three flips form one chain, depths 0/1/2", [r["depth"] for r in rows[:3]] == [0, 1, 2])
check("a fresh entry after a take-profit starts over", rows[3]["depth"] == 0)
check("the first three share one chain id", len({r["chain"] for r in rows[:3]}) == 1)
check("the fourth is its own chain", rows[3]["chain"] != rows[0]["chain"])
check("the chain's own net is counted whole",
      abs(sum(r["profit"] for r in rows[:3]) - (-86.52)) < 0.005)
check("$/oz divides by the real lot size",
      abs(rows[0]["oz"] - (-57.06 / 6.0)) < 1e-9)

print("\nwhat must NOT be called a flip")
same = build_chains([
    trade("SELL", T(5, 21), T(5, 33), SWAP),
    trade("SELL", T(5, 33), T(5, 57), SWAP),
])
check("same direction is not a reversal", same[1]["depth"] == 0)

late = build_chains([
    trade("SELL", T(5, 21), T(5, 33), SWAP),
    trade("BUY", T(5, 40), T(5, 57), SWAP),
])
check("an entry 7 minutes later is not a reversal", late[1]["depth"] == 0)

after_tp = build_chains([
    trade("SELL", T(5, 21), T(5, 33), "Take Profit"),
    trade("BUY", T(5, 33), T(5, 57), SWAP),
])
check("an instant entry after a TP is not a reversal", after_tp[1]["depth"] == 0)

deep = build_chains([trade("SELL" if i % 2 == 0 else "BUY",
                           T(5, 21) + timedelta(minutes=6 * i),
                           T(5, 21) + timedelta(minutes=6 * (i + 1)), SWAP)
                     for i in range(5)])
check("a five-trade chain reaches depth 4", [r["depth"] for r in deep] == [0, 1, 2, 3, 4])
check("and stays one chain", len({r["chain"] for r in deep}) == 1)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
