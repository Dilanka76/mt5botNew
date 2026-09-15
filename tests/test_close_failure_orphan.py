"""A failed close must not orphan the trade.

    python3 tests/test_close_failure_orphan.py

REAL INCIDENT, demo1_m3, 2026-09-14. The breakeven armed at 03:00:41 and
moved the stop to 4330.29. Price ticked to 4330.49 -- a stop hit on a
SELL -- so the engine called _close_position, which did this:

    position = self.position
    self.position = None                             # forget it
    self.executor.close_position(position.ticket)    # <-- raised
    log_decision(..., "trade_exited", ...)           # never ran

The engine was flat, the broker still held the trade, and nothing was
logged because the log line came after the throw. main.py caught the
exception, printed "Error in main loop iteration", and carried on. The
next heartbeat said state=IDLE and every heartbeat for the next 2h32m
said the same.

The trade could not recover: reconcile_on_startup() only runs at launch.
And one second earlier the TP-runner had deleted the broker take-profit,
so the orphan had no target, no software stop, and nothing watching it.
It ran +$13.19 in profit, untouched, and was back to a loss when found.

Two fixes, both checked here: close BEFORE clearing state, and heal a
desync mid-run instead of waiting for a restart.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, ".")
_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
_stub.ORDER_TYPE_BUY = 0
_stub.ORDER_TYPE_SELL = 1
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

from bot.strategy.state_machine_dual_cross import DualPosition
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINES = sorted((ROOT / "bot" / "strategy").glob("state_machine*.py"))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Direction:
    BUY = types.SimpleNamespace(value="BUY")
    SELL = types.SimpleNamespace(value="SELL")


class BrokenExecutor:
    """close_position always fails, exactly as it did on the day."""
    def __init__(self):
        self.close_attempts = 0

    def close_position(self, ticket):
        self.close_attempts += 1
        raise RuntimeError("order_send returned None (broker rejected the close)")


class Executor:
    def __init__(self, position=None):
        self._position = position
        self.close_attempts = 0

    def close_position(self, ticket):
        self.close_attempts += 1

    def get_open_position(self):
        return self._position


def engine_with(executor, position):
    e = DualCrossConfirmedSwapAdxEngine.__new__(DualCrossConfirmedSwapAdxEngine)
    e.recently_closed = {}   # __init__ bypassed; see test_swap_reentry_race.py
    e.executor = executor
    e.position = position
    e.config = types.SimpleNamespace(
        symbol="XAUUSDp", stop_loss_usd=7.0, take_profit_usd=6.0,
        execution=types.SimpleNamespace(mode="live_execute"),
    )
    e.state = None
    return e


def a_position():
    return DualPosition(
        direction=Direction.SELL, ticket=203845694, entry_price=4335.19,
        take_profit=4329.19, stop_loss=4330.29,
    )


def main() -> None:
    print("a close that raises keeps the position")
    broken = BrokenExecutor()
    engine = engine_with(broken, a_position())
    try:
        engine._close_position(category="stop_loss", reason="stop hit", exit_price=4330.29)
    except RuntimeError:
        pass
    else:
        check("the exception still propagates (main.py must see it)", False)
    check("the exception still propagates (main.py must see it)", True)
    check("the broker close WAS attempted", broken.close_attempts == 1)
    check("the engine still holds the position -- NOT orphaned",
          engine.position is not None and engine.position.ticket == 203845694)

    print("\na close that succeeds still clears the position")
    ok = Executor()
    engine = engine_with(ok, a_position())
    try:
        engine._close_position(category="stop_loss", reason="stop hit", exit_price=4330.29)
    except Exception as exc:      # log_decision needs logging configured
        if "setup_logging" not in str(exc):
            raise
    check("the broker close was attempted", ok.close_attempts == 1)
    check("the position is cleared after a successful close", engine.position is None)

    print("\nevery engine closes before it forgets")
    # Parsed, not grepped. The first version of this check just looked for
    # the nearest "self.position = None" to the close call, which falsely
    # accused state_machine.py (it uses self.open_position, and was already
    # correct) while a real bug in state_machine_dual_cross.py wore a
    # different shape entirely: self.positions.pop(direction). Walking the
    # method body finds the state change whatever it is called.
    import ast

    def state_changes(node):
        """Line numbers where this method drops its record of the position."""
        lines = []
        for n in ast.walk(node):
            # self.<attr> = None
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                    and n.value.value is None:
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                            and t.value.id == "self":
                        lines.append(n.lineno)
            # self.<dict>.pop(...)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "pop" \
                    and isinstance(n.func.value, ast.Attribute) \
                    and isinstance(n.func.value.value, ast.Name) \
                    and n.func.value.value.id == "self":
                lines.append(n.lineno)
        return lines

    def close_calls(node):
        lines = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "close_position" \
                    and isinstance(n.func.value, ast.Attribute) \
                    and n.func.value.attr == "executor":
                lines.append(n.lineno)
        return lines

    checked = 0
    for path in ENGINES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_close"):
                continue
            closes, changes = close_calls(node), state_changes(node)
            if not closes or not changes:
                continue
            checked += 1
            check(f"{path.name}:{node.name} drops the position only after the close",
                  min(changes) > max(closes))
    check("the scan actually found close methods to check", checked >= 12)

    print("\nmid-run desync heals without a restart")
    broker_position = types.SimpleNamespace(
        type=_stub.ORDER_TYPE_SELL, ticket=203845694, price_open=4335.19, tp=0.0)
    engine = engine_with(Executor(broker_position), None)
    engine._compute_stop_loss = lambda d, p: p + 7.0
    engine._update_state = lambda: None
    try:
        engine._self_heal_desync()
    except Exception as exc:
        if "setup_logging" not in str(exc):
            raise
    check("a forgotten broker position is adopted",
          engine.position is not None and engine.position.ticket == 203845694)

    print("\nit does not adopt when there is nothing to adopt")
    engine = engine_with(Executor(None), None)
    engine._self_heal_desync()
    check("no broker position -> stays flat", engine.position is None)

    print("\nit never overwrites a position the engine already has")
    held = a_position()
    engine = engine_with(Executor(broker_position), held)
    engine._self_heal_desync()
    check("an engine that is NOT flat is left alone", engine.position is held)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
