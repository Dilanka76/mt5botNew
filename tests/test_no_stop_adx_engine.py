"""The ADX engine must run demo2_m5's no-stop design without crashing.

    python3 tests/test_no_stop_adx_engine.py

User's decision, 2026-09-16: run live2 on demo2's strategy with no software
stop on either leg, bounded only by the broker backstop, supervised by hand.

demo2_m3 runs the plain `swap` engine, which has always tolerated a null
stop. demo2_m5 runs `swap_adx`, which never had to -- and it compares
`tick.bid <= position.stop_loss` with no None check, so a null stop would
raise TypeError on EVERY tick. bot/config.py refused to load such a config
at all, which is what stopped the 2026-09-16 clone. That refusal was right;
the fix is to make the engine actually support it, not to bypass the guard.

Three things are checked here, and the guard rule is the same in all three
places (engine constructor, config loader, clone_strategy): refuse when
NOTHING bounds the trade -- no software stop AND no broker backstop -- not
merely when the software stop is absent. A backstop lives at the broker, so
it survives the bot dying, which a software stop does not.
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

from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import (
    DualCrossConfirmedSwapAdxEngine as Engine,
)
# The REAL enum. A look-alike SimpleNamespace(value="BUY") is not equal to
# Direction.BUY, so every `direction == Direction.BUY` in the engine falls
# through to the SELL branch and the test silently checks the wrong path --
# which is exactly what the first version of this file did.
from bot.strategy.state_machine import Direction

ROOT = Path(__file__).resolve().parents[1]
CONFIG_SRC = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
CLONE_SRC = (ROOT / "scripts" / "clone_strategy.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def engine(stop, backstop):
    e = Engine.__new__(Engine)
    e.config = types.SimpleNamespace(
        symbol="XAUUSD", stop_loss_usd=stop, take_profit_usd=8.0,
        broker_backstop_usd=backstop, breakeven_trigger_usd=None,
        tp_runner_trail_usd=None,
    )
    return e


def main() -> None:
    print("_compute_stop_loss")
    e = engine(None, 35.0)
    check("a null stop yields NO stop price",
          Engine._compute_stop_loss(e, Direction.BUY, 4330.0) is None)
    e2 = engine(10.0, 35.0)
    check("a real stop still computes for a BUY",
          Engine._compute_stop_loss(e2, Direction.BUY, 4330.0) == 4320.0)
    check("a real stop still computes for a SELL",
          Engine._compute_stop_loss(e2, Direction.SELL, 4330.0) == 4340.0)
    check("an explicit distance still overrides",
          Engine._compute_stop_loss(e2, Direction.BUY, 4330.0, 5.0) == 4325.0)

    print("\nthe stop-hit test never compares against None")
    src = (ROOT / "bot" / "strategy"
           / "state_machine_dual_cross_confirmed_swap_adx.py").read_text(encoding="utf-8")
    check("stop_hit guards on `is not None` first",
          "stop_hit = position.stop_loss is not None and (" in src)
    # Prove it, don't just read it: a float <= None raises TypeError.
    try:
        _ = 4330.0 <= None
        raised = False
    except TypeError:
        raised = True
    check("(and a float<=None really would raise TypeError)", raised)

    print("\nthe pending-reversal tightening does not halve a missing stop")
    check("it branches on stop_loss_usd being None",
          "elif self.config.stop_loss_usd is None:" in src)
    check("and says so rather than silently skipping",
          "swap_pending_no_stop_to_tighten" in src)

    print("\nall three guards agree: refuse only when NOTHING bounds the trade")
    check("engine constructor: null stop needs a backstop",
          "config.stop_loss_usd is None and not config.broker_backstop_usd" in src)
    check("config loader: same rule",
          'raw.get("stop_loss_usd") is None and not raw.get("broker_backstop_usd")' in CONFIG_SRC)
    check("clone_strategy: same rule",
          'src.get("stop_loss_usd") is None or args.no_software_stop' in CLONE_SRC
          and 'backstop = dst.get("broker_backstop_usd")' in CLONE_SRC)

    print("\nthe constructor still refuses a genuinely unprotected config")
    try:
        Engine.__init__(engine(None, None), types.SimpleNamespace(
            stop_loss_usd=None, broker_backstop_usd=None), None, None)
        refused = False
    except ValueError as exc:
        refused = "needs broker_backstop_usd" in str(exc)
    except Exception:
        refused = False
    check("no stop AND no backstop -> ValueError", refused)

    print("\nclone_strategy removes fields the source does not set")
    check("it collects stale keys", "stale = [k for k in STRATEGY_FIELDS" in CLONE_SRC)
    check("and pops them before writing", "dst.pop(key, None)" in CLONE_SRC)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
