"""Proves the daily loss limit actually BLOCKS a real entry.

    python3 tests/test_daily_loss_engine.py

tests/test_daily_loss.py checks the arithmetic. This checks the wiring:
that _enter() -- the single funnel every entry path goes through in both
live engines -- refuses to place an order once the cap is hit, and still
places one when it is not. A correct calculation wired to nothing would
protect no money at all.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

if "MetaTrader5" not in sys.modules:
    _stub = types.ModuleType("MetaTrader5")
    for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
        setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
    sys.modules["MetaTrader5"] = _stub

import bot.daily_loss as daily_loss
from bot.daily_loss import COLOMBO
from bot.strategy.cross_detector import Direction

ENGINES = {
    "dual_cross_confirmed_swap": "bot.strategy.state_machine_dual_cross_confirmed_swap",
    "dual_cross_confirmed_swap_adx": "bot.strategy.state_machine_dual_cross_confirmed_swap_adx",
}


@dataclass
class FakeLogging:
    log_dir: str = "logs"


@dataclass
class FakeExec:
    magic_number: int = 1
    mode: str = "demo_execute"


@dataclass
class FakeConfig:
    account: str
    daily_loss_limit_usd: float | None
    symbol: str = "XAUUSDp"
    logging: FakeLogging = field(default_factory=FakeLogging)
    execution: FakeExec = field(default_factory=FakeExec)
    position_sizing: list = field(default_factory=list)


class FakeConnector:
    def account_info(self):
        raise AssertionError("account_info() called -- the entry was NOT blocked!")


class FakeExecutor:
    """Answers the broker-side duplicate check added 2026-09-08.
    `positions` non-empty means the broker already shows one."""

    def __init__(self, positions=()):
        self.positions = list(positions)

    def get_open_positions(self):
        return self.positions


def write_ledger(tmp: Path, account: str, profits: list[float]) -> None:
    d = tmp / "logs" / account
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(COLOMBO)
    with (d / "trade_history.jsonl").open("w") as f:
        for i, p in enumerate(profits):
            f.write(json.dumps({
                "ticket": i + 1, "profit": p,
                "close_time": (now - timedelta(minutes=i + 1)).isoformat(),
            }) + "\n")


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("daily loss limit -- engine wiring")
    import importlib

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        daily_loss.PROJECT_ROOT = tmp

        for variant, module_path in ENGINES.items():
            mod = importlib.import_module(module_path)
            logged: list = []
            mod.log_decision = lambda symbol, action, reason, **kw: logged.append(action)
            cls = next(v for k, v in vars(mod).items() if k.endswith("Engine") and isinstance(v, type))

            account = f"acct_{variant}"
            write_ledger(tmp, account, [-30.0, -25.0])   # -$55 today

            # Over the cap: _enter must refuse BEFORE touching the broker.
            eng = object.__new__(cls)
            eng.config = FakeConfig(account=account, daily_loss_limit_usd=50.0)
            eng.connector = FakeConnector()   # raises if reached
            eng.executor = FakeExecutor()
            eng._daily_limit_logged_date = None
            result = eng._enter(Direction.BUY, reason="test")
            check(f"{variant}: -$55 vs a $50 cap -> entry refused, broker never touched",
                  result is None)
            check(f"{variant}: logs daily_loss_limit_hit",
                  logged.count("daily_loss_limit_hit") == 1)

            # Logged once per day, not once per candle.
            eng._enter(Direction.BUY, reason="test")
            eng._enter(Direction.SELL, reason="test")
            check(f"{variant}: still blocked, but logged only once",
                  logged.count("daily_loss_limit_hit") == 1)

            # Under the cap: the guard must NOT block -- proven by the
            # connector being reached (it raises, which is the pass here).
            eng2 = object.__new__(cls)
            eng2.config = FakeConfig(account=account, daily_loss_limit_usd=500.0)
            eng2.connector = FakeConnector()
            eng2.executor = FakeExecutor()
            eng2._daily_limit_logged_date = None
            reached = False
            try:
                eng2._enter(Direction.BUY, reason="test")
            except AssertionError:
                reached = True
            check(f"{variant}: -$55 vs a $500 cap -> entry proceeds", reached)

            # Limit unset: the rule must be completely inert.
            eng3 = object.__new__(cls)
            eng3.config = FakeConfig(account=account, daily_loss_limit_usd=None)
            eng3.connector = FakeConnector()
            eng3.executor = FakeExecutor()
            eng3._daily_limit_logged_date = None
            reached = False
            try:
                eng3._enter(Direction.BUY, reason="test")
            except AssertionError:
                reached = True
            check(f"{variant}: no limit configured -> rule inert", reached)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
