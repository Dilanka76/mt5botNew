"""Logic checks for the live TP-runner in
bot/strategy/state_machine_dual_cross_confirmed_swap_adx.py.

    python3 tests/test_tp_runner_engine.py

This drives the REAL _manage_tp_runner() method against a fake executor
and fake ticks. It exists because this is live trading code that moves
real stops: a bug here does not produce a wrong number in a report, it
mismanages an open position.

The engine is built with object.__new__ and its attributes set directly,
so the method under test is exactly the shipped one without needing a
live MT5 connection, config file or broker.
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass

sys.path.insert(0, ".")

# MetaTrader5 is Windows-only; stub the constants read at import time.
if "MetaTrader5" not in sys.modules:
    _stub = types.ModuleType("MetaTrader5")
    for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
        setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
    sys.modules["MetaTrader5"] = _stub

import bot.strategy.state_machine_dual_cross_confirmed_swap_adx as engine_mod
from bot.strategy.cross_detector import Direction
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine

# log_decision writes to real log files; capture instead.
DECISIONS: list[tuple] = []
engine_mod.log_decision = lambda symbol, action, reason, **kw: DECISIONS.append((action, reason))


@dataclass
class FakeConfig:
    take_profit_usd: float
    tp_runner_trail_usd: float | None
    tp_runner_arm_before_usd: float = 0.20
    symbol: str = "XAUUSDp"


class FakeExecutor:
    """Records every broker call and can be told to reject them."""

    def __init__(self, succeed: bool = True):
        self.calls: list[dict] = []
        self.succeed = succeed

    def set_sltp(self, ticket, stop_loss, take_profit):
        self.calls.append({"ticket": ticket, "sl": stop_loss, "tp": take_profit})
        return self.succeed


class FakePosition:
    def __init__(self, direction: Direction, entry: float, stop: float, key: float = 1.0):
        self.direction = direction
        self.entry_price = entry
        self.stop_loss = stop
        self.ticket = 12345
        self.opened_monotonic = key


class FakeTick:
    def __init__(self, bid: float):
        self.bid = bid


def make_engine(tp: float, trail: float | None, succeed: bool = True):
    eng = object.__new__(DualCrossConfirmedSwapAdxEngine)
    eng.config = FakeConfig(take_profit_usd=tp, tp_runner_trail_usd=trail)
    eng.executor = FakeExecutor(succeed)
    eng.runner_key = None
    eng.runner_tp_removed = False
    eng.runner_locked = False
    eng.runner_best = 0.0
    eng.runner_broker_stop = None
    return eng


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("TP-runner engine logic checks")

    # --- disabled -----------------------------------------------------
    eng = make_engine(tp=5.0, trail=None)
    pos = FakePosition(Direction.BUY, 4400.0, 4395.0)
    eng._manage_tp_runner(pos, FakeTick(4410.0))
    check("trail unset -> rule is completely inert",
          eng.executor.calls == [] and pos.stop_loss == 4395.0)

    # --- below the arm point ------------------------------------------
    eng = make_engine(tp=5.0, trail=2.0)
    pos = FakePosition(Direction.BUY, 4400.0, 4395.0)
    eng._manage_tp_runner(pos, FakeTick(4404.5))   # +4.50, arm point is +4.80
    check("below the arm point -> nothing touched",
          eng.executor.calls == [] and not eng.runner_tp_removed)

    # --- arm ----------------------------------------------------------
    eng._manage_tp_runner(pos, FakeTick(4404.8))   # +4.80 == tp - 0.20
    check("at the arm point -> broker take-profit cleared (tp=0.0)",
          len(eng.executor.calls) == 1 and eng.executor.calls[0]["tp"] == 0.0
          and eng.executor.calls[0]["sl"] is None)
    check("arming does not move the stop yet", pos.stop_loss == 4395.0)
    check("arming is not repeated on later ticks",
          (eng._manage_tp_runner(pos, FakeTick(4404.9)), len(eng.executor.calls) == 1)[1])

    # --- lock ---------------------------------------------------------
    eng._manage_tp_runner(pos, FakeTick(4405.0))   # +5.00
    check("at the target -> stop locked AT the target price", pos.stop_loss == 4405.0)
    check("lock is pushed to the broker as a real stop",
          eng.executor.calls[-1]["sl"] == 4405.0 and eng.executor.calls[-1]["tp"] is None)
    check("locked profit equals the old take-profit exactly",
          pos.stop_loss - pos.entry_price == 5.0)

    # --- trail --------------------------------------------------------
    eng._manage_tp_runner(pos, FakeTick(4406.0))   # +6.00, 6-2=4 < tp -> no move
    check("run smaller than the trail -> stop stays at the lock", pos.stop_loss == 4405.0)

    eng._manage_tp_runner(pos, FakeTick(4410.0))   # +10.00, 10-2=8 -> stop at +8
    check("bigger run -> stop trails to best minus trail", pos.stop_loss == 4408.0)

    eng._manage_tp_runner(pos, FakeTick(4406.0))   # pullback
    check("pullback does NOT move the stop back down", pos.stop_loss == 4408.0)

    eng._manage_tp_runner(pos, FakeTick(4415.0))   # new high +15 -> stop +13
    check("new high ratchets the stop up again", pos.stop_loss == 4413.0)

    # --- broker-call throttling ---------------------------------------
    before = len(eng.executor.calls)
    eng._manage_tp_runner(pos, FakeTick(4415.05))  # +0.05 improvement only
    check("software stop follows a tiny improvement",
          abs(pos.stop_loss - 4413.05) < 1e-9)
    check("but the broker stop is NOT re-sent for < $0.10",
          len(eng.executor.calls) == before)

    # --- SELL mirrored -------------------------------------------------
    eng = make_engine(tp=6.0, trail=2.0)
    pos = FakePosition(Direction.SELL, 4400.0, 4410.0)
    eng._manage_tp_runner(pos, FakeTick(4394.0))   # +6.00 for a SELL
    check("SELL locks at entry MINUS the target", pos.stop_loss == 4394.0)
    eng._manage_tp_runner(pos, FakeTick(4390.0))   # +10 -> stop at +8 => 4392
    check("SELL trails downward", pos.stop_loss == 4392.0)
    eng._manage_tp_runner(pos, FakeTick(4395.0))   # pullback
    check("SELL stop does not move back up", pos.stop_loss == 4392.0)

    # --- a new position must not inherit the previous one's state -----
    pos2 = FakePosition(Direction.SELL, 4300.0, 4310.0, key=2.0)
    eng._manage_tp_runner(pos2, FakeTick(4299.0))  # only +1.00
    check("new position resets the runner state",
          not eng.runner_locked and pos2.stop_loss == 4310.0)

    # --- broker rejects the modify -------------------------------------
    eng = make_engine(tp=5.0, trail=2.0, succeed=False)
    pos = FakePosition(Direction.BUY, 4400.0, 4395.0)
    eng._manage_tp_runner(pos, FakeTick(4405.0))
    check("a rejected broker modify still moves the software stop",
          pos.stop_loss == 4405.0)
    check("a rejected modify does not raise and does not stall the rule",
          eng.runner_locked is True)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
