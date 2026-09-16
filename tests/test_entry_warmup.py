"""A restarting bot must not enter on a cross that fired while it was down.

    python3 tests/test_entry_warmup.py

REAL INCIDENT, live2_m5, 2026-09-16 22:43:14, real money:

    22:43:12   Bot started ... state=IDLE
    22:43:13   HEARTBEAT   state=IDLE
    22:43:14   Order opened: SELL 0.02 @ 4273.30

Two seconds. The cross it cited had closed at 22:40 -- before the process
existed. MT5's candle cache is briefly one behind at connect, so the first
loop evaluated candle N-2 (prev_ema13 was None, correctly no entry, prev set
to N-2) and the loop one second later evaluated N-1 against it and treated a
historical cross as live. The `prev_ema13 is None` guard only ever blocked
the FIRST of those two evaluations.

The tell was the gap -- how far price had already run from EMA13 at entry:

    normal live entries   2.75, 2.85, 2.87
    startup entries      10.08, 10.16

Ten dollars of the move already gone, on trades targeting $6-$10. The user
spotted it twice from the screen; both times the code was read and both
times the reading was wrong.

FIX: no ENTRY until one full candle period has passed since startup, so any
cross acted on provably closed while the bot was watching. EXITS are not
gated -- closing a position on a cross that fired while the bot was down is
the safe direction, and only the re-entry after it is withheld.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, ".")
_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
_stub.ORDER_TYPE_BUY, _stub.ORDER_TYPE_SELL = 0, 1
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

from bot.strategy.state_machine_dual_cross_confirmed_swap import DualCrossConfirmedSwapEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINES = {
    "swap": ROOT / "bot" / "strategy" / "state_machine_dual_cross_confirmed_swap.py",
    "swap_adx": ROOT / "bot" / "strategy" / "state_machine_dual_cross_confirmed_swap_adx.py",
}

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def warmed(cls, timeframe_seconds, age_seconds):
    e = cls.__new__(cls)
    e._started_monotonic = time.monotonic() - age_seconds
    e._warmup_seconds = timeframe_seconds
    return cls._warmed_up(e)


def main() -> None:
    print("the warm-up window itself")
    for name, cls in (("swap", DualCrossConfirmedSwapEngine),
                      ("swap_adx", DualCrossConfirmedSwapAdxEngine)):
        check(f"{name}: 2 seconds after startup on M5 -> NOT warmed up",
              warmed(cls, 300, 2) is False)
        check(f"{name}: 2 seconds after startup on M3 -> NOT warmed up",
              warmed(cls, 180, 2) is False)
        check(f"{name}: one second SHORT of a full M5 candle -> still not warmed",
              warmed(cls, 300, 299) is False)
        check(f"{name}: a full M5 candle later -> warmed up",
              warmed(cls, 300, 300) is True)
        check(f"{name}: well past it -> warmed up",
              warmed(cls, 300, 3600) is True)

    print("\nthe real incident: 2s after start, M5")
    check("would now be refused",
          warmed(DualCrossConfirmedSwapAdxEngine, 300, 2) is False)

    print("\nevery ENTRY path is gated, in both engines")
    for name, path in ENGINES.items():
        src = path.read_text(encoding="utf-8")
        check(f"{name}: warm-up is checked before entering",
              src.count("if not self._warmed_up():") == 2)
        check(f"{name}: and it says so rather than skipping silently",
              src.count("entry_skipped_warmup") == 2)
        check(f"{name}: the window is one candle of THIS timeframe",
              "minutes_for(config.timeframe) * 60" in src)

    print("\nEXITS are deliberately NOT gated")
    for name, path in ENGINES.items():
        src = path.read_text(encoding="utf-8")
        # the close must be emitted before the guard on the swap path
        close_at = src.index('category="swapped')
        guard_at = src.index("if not self._warmed_up():")
        check(f"{name}: the position still closes on a stale cross",
              close_at < guard_at)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
