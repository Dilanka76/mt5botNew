"""Logic checks for scripts/simulate_tp_runner.py's simulate().

    python3 tests/test_tp_runner.py

Verifies the ratchet, the locked floor, the unchanged opposite-cross
exit, and the deliberately pessimistic within-candle ordering -- before
any conclusion is drawn from it on real trades.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

# The MetaTrader5 package ships Windows wheels only and can never import
# on the dev Mac. simulate() itself touches none of it -- the import
# chain does -- so a bare stub is enough to load the module and test the
# pure logic. Established practice on this project; see
# feedback_testing_windows_only_mt5.
if "MetaTrader5" not in sys.modules:
    import types
    _stub = types.ModuleType("MetaTrader5")
    # Only the TIMEFRAME_* constants are read at import time (see
    # bot/mt5_connector.py's module-level table). Values are arbitrary --
    # nothing here ever reaches a real terminal.
    for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
        setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
    sys.modules["MetaTrader5"] = _stub

spec = importlib.util.spec_from_file_location("tp_runner", "scripts/simulate_tp_runner.py")
tp_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp_runner)
simulate = tp_runner.simulate

START = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def frame(candles: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """candles: (high, low, close, ema13, ema21), one per minute from START."""
    idx = pd.date_range(start=START, periods=len(candles), freq="1min", tz=timezone.utc)
    return pd.DataFrame(
        {"high": [c[0] for c in candles], "low": [c[1] for c in candles],
         "close": [c[2] for c in candles], "ema13": [c[3] for c in candles],
         "ema21": [c[4] for c in candles]}, index=idx)


def check(label, got, want):
    """Compares the profit leg with a tolerance -- these are floating
    point price differences, so 5.799999999999997 is the right answer."""
    if got is None or want is None:
        ok = got is want
    else:
        ok = got[0] == want[0] and abs(got[1] - want[1]) < 1e-6
    assert ok, f"{label}: got {got!r}, wanted {want!r}"
    print(f"  OK  {label}")


def main() -> None:
    print("simulate_tp_runner logic checks")
    before = START - timedelta(minutes=1)
    UP = (2.0, 1.0)  # ema13 > ema21 -> BUY trend, no cross

    # Ratchet: rises to +6.00, trail 0.50 lifts the stop to 5.50, then a
    # pullback takes it out there -- ABOVE the 4.50 lock.
    df = frame([(106.0, 104.6, 105.0, *UP), (105.6, 105.4, 105.5, *UP)])
    check("trail ratchets up, exits at 5.50 not the 4.50 lock",
          simulate(df, before, "BUY", 100.0, 4.50, 0.50, 50), ("trailed out", 5.50))

    # Immediate pullback: never advances, so the locked floor holds.
    df = frame([(105.2, 104.0, 104.2, *UP)])
    check("no advance -> stopped at the lock, still a WIN",
          simulate(df, before, "BUY", 100.0, 4.50, 0.50, 50), ("stopped at lock", 4.50))

    # Pessimistic ordering: this candle both reaches +7 and dips to +4.4.
    # The dip must be taken first, so it stops at the lock rather than
    # ratcheting to 6.50.
    df = frame([(107.0, 104.4, 106.0, *UP)])
    check("adverse extreme assumed first within a candle",
          simulate(df, before, "BUY", 100.0, 4.50, 0.50, 50), ("stopped at lock", 4.50))

    # Opposite cross closes at that candle's close, unchanged rule.
    df = frame([(106.0, 105.0, 105.0, *UP), (106.5, 105.2, 105.8, 1.0, 2.0)])
    check("opposite cross exits at its close (+5.80)",
          simulate(df, before, "BUY", 100.0, 4.50, None, 50), ("opposite cross", 5.80))

    # No trail: the stop never moves, so a big run gives it all back to lock.
    df = frame([(110.0, 105.0, 109.0, *UP), (109.0, 104.0, 104.5, *UP)])
    check("lock-only gives back the run down to the lock",
          simulate(df, before, "BUY", 100.0, 4.50, None, 50), ("stopped at lock", 4.50))

    # SELL is mirrored: entry 100, price falls to 94 -> +6 profit.
    DOWN = (1.0, 2.0)
    df = frame([(95.4, 94.0, 94.5, *DOWN), (94.6, 94.4, 94.5, *DOWN)])
    check("SELL mirrored, trails to 5.50",
          simulate(df, before, "SELL", 100.0, 4.50, 0.50, 50), ("trailed out", 5.50))

    # Stepped trail (the user's "$1 to $1" form): the stop jumps up a
    # whole $1 only once the best price has gained a full $1 past the
    # lock, so it lags a continuous trail and gives price more room.
    # Best reaches +6.00, i.e. 1.50 past the 4.50 lock -> one full step ->
    # stop = 5.50. The pullback to +5.40 then takes it out there.
    df = frame([(106.0, 104.6, 105.0, *UP), (105.6, 105.4, 105.5, *UP)])
    check("$1 steps: one full step taken, exits at 5.50",
          simulate(df, before, "BUY", 100.0, 4.50, None, 50, 1.00), ("trailed out", 5.50))

    # With $3 steps the same +1.50 of gain is not a full step, so the stop
    # never leaves the lock. A third candle is needed for it to actually
    # fall back that far -- with only the two above the trade is still
    # open and correctly returns None.
    df3 = frame([(106.0, 104.6, 105.0, *UP), (105.6, 105.4, 105.5, *UP),
                 (105.5, 104.0, 104.1, *UP)])
    check("$3 steps: gain under one step, stop stays at the lock",
          simulate(df3, before, "BUY", 100.0, 4.50, None, 50, 3.00), ("stopped at lock", 4.50))

    # A step must not move the stop ABOVE the best price seen.
    df = frame([(107.4, 105.0, 107.0, *UP), (107.0, 104.0, 104.2, *UP)])
    check("$1 steps never overshoot the best price",
          simulate(df, before, "BUY", 100.0, 4.50, None, 50, 1.00), ("trailed out", 6.50))

    # Still running at the horizon -> excluded, not guessed. Each candle
    # must climb faster than the trail, otherwise the stop legitimately
    # catches up and the trade resolves (the first version of this test
    # used flat candles and was simply wrong).
    df = frame([(106.0, 105.5, 105.8, *UP), (107.0, 106.5, 106.8, *UP),
                (108.0, 107.5, 107.8, *UP), (109.0, 108.5, 108.8, *UP),
                (110.0, 109.5, 109.8, *UP)])
    check("still running at the horizon -> None", simulate(df, before, "BUY", 100.0, 4.50, 0.50, 3), None)

    # Nothing after the TP moment at all.
    check("no candles after the exit -> None",
          simulate(frame([(106.0, 105.0, 105.5, *UP)]), START + timedelta(hours=2),
                   "BUY", 100.0, 4.50, 0.50, 50), None)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
