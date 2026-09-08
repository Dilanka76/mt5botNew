"""_enter() must refuse when the broker already shows our position.

    python3 tests/test_duplicate_position_guard.py

On 2026-09-08 two main.py processes for demo2_m1 each opened a SELL one
second apart -- 0.12 lots became 0.24 on an account meant to hold one.
The OS mutex now stops a duplicate PROCESS; this guard stops a duplicate
POSITION even if one somehow runs, which is the part that actually costs
money.

Drives the REAL _enter() in both live engines with a fake executor.
"""
from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field

sys.path.insert(0, ".")

if "MetaTrader5" not in sys.modules:
    _stub = types.ModuleType("MetaTrader5")
    for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
        setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
    sys.modules["MetaTrader5"] = _stub

from bot.strategy.cross_detector import Direction

ENGINES = {
    "dual_cross_confirmed_swap": "bot.strategy.state_machine_dual_cross_confirmed_swap",
    "dual_cross_confirmed_swap_adx": "bot.strategy.state_machine_dual_cross_confirmed_swap_adx",
}


@dataclass
class FakeExec:
    magic_number: int = 920001
    mode: str = "demo_execute"


@dataclass
class FakeLogging:
    log_dir: str = "logs"


@dataclass
class FakeConfig:
    account: str = "demo2_m1"
    symbol: str = "XAUUSDp"
    daily_loss_limit_usd: float | None = None
    execution: FakeExec = field(default_factory=FakeExec)
    logging: FakeLogging = field(default_factory=FakeLogging)


class FakePosition:
    def __init__(self, ticket: int):
        self.ticket = ticket


class FakeExecutor:
    def __init__(self, positions=()):
        self.positions = list(positions)

    def get_open_positions(self):
        return self.positions


class ExplodingConnector:
    def account_info(self):
        raise AssertionError("reached the broker -- the entry was NOT blocked!")


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("duplicate-position guard")
    for variant, path in ENGINES.items():
        mod = importlib.import_module(path)
        logged: list = []
        mod.log_decision = lambda symbol, action, reason, **kw: logged.append(action)
        cls = next(v for k, v in vars(mod).items()
                   if k.endswith("Engine") and isinstance(v, type))

        # A position already at the broker -> refuse, before touching it.
        eng = object.__new__(cls)
        eng.config = FakeConfig()
        eng.executor = FakeExecutor([FakePosition(111), FakePosition(222)])
        eng.connector = ExplodingConnector()
        eng._daily_limit_logged_date = None
        check(f"{variant}: broker shows a position -> entry refused",
              eng._enter(Direction.SELL, reason="test") is None)
        check(f"{variant}: and it is logged as entry_blocked_existing_position",
              logged.count("entry_blocked_existing_position") == 1)

        # Flat at the broker -> proceeds (proven by the connector raising).
        eng2 = object.__new__(cls)
        eng2.config = FakeConfig()
        eng2.executor = FakeExecutor([])
        eng2.connector = ExplodingConnector()
        eng2._daily_limit_logged_date = None
        reached = False
        try:
            eng2._enter(Direction.SELL, reason="test")
        except AssertionError:
            reached = True
        check(f"{variant}: broker flat -> entry proceeds normally", reached)

        # A broker READ failure must not block trading -- the mutex is
        # the primary guard and an outage on every transient error would
        # be its own problem.
        class Broken:
            def get_open_positions(self):
                raise RuntimeError("MT5 query failed")

        eng3 = object.__new__(cls)
        eng3.config = FakeConfig()
        eng3.executor = Broken()
        eng3.connector = ExplodingConnector()
        eng3._daily_limit_logged_date = None
        reached = False
        try:
            eng3._enter(Direction.SELL, reason="test")
        except AssertionError:
            reached = True
        check(f"{variant}: a broker read failure does not block the entry", reached)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
