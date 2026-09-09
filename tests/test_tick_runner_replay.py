"""Tick-by-tick runner replay: the ORDER of prices must decide the exit.

    python3 tests/test_tick_runner_replay.py      (needs pandas)

Candle mode has to guess whether a candle's high or its low came first,
which is why it runs twice and is read as a pessimistic/optimistic range.
Ticks are the order, so this must return one answer -- and crucially, two
tick streams containing the SAME prices in a different order must give
DIFFERENT results. A replay that returns the same number for both is not
using the order at all, which would silently make it worthless.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

import pandas as pd

from simulate_tp_runner import simulate_on_ticks

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def ticks(prices: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-09-09T00:00Z", periods=len(prices), freq="1s")
    return pd.DataFrame({"bid": prices, "ask": [p + 0.10 for p in prices]}, index=idx)


def main() -> None:
    print("tick-by-tick runner replay")
    entry, lock, trail = 100.0, 5.0, 0.50

    # Rise to +6.5, THEN fall to +5.6. The stop has ratcheted to +6.0 by
    # the time the fall arrives, so it must stop out at +6.00. This is the
    # exact case the user raised: a $0.50 flick taking the trade out.
    up_then_down = simulate_on_ticks(ticks([105.5, 106.5, 105.6]), "BUY", entry,
                                     lock, trail, None, None, None)
    check("rise then fall -> trailed out at +6.00",
          up_then_down == ("trailed out", 6.0))

    # The SAME three prices, reordered so the small dip arrives BEFORE the
    # spike. The stop is still at the +5.00 lock when +5.6 passes, so it
    # survives, and the trade is still open when the ticks run out.
    down_then_up = simulate_on_ticks(ticks([105.5, 105.6, 106.5]), "BUY", entry,
                                     lock, trail, None, None, None)
    check("dip before spike -> survives, still open at the end", down_then_up is None)
    check("SAME prices in a different ORDER give different answers",
          up_then_down != down_then_up)

    # A dip below the lock itself ends it at the lock, not lower.
    check("a drop through the lock exits AT the lock, never below",
          simulate_on_ticks(ticks([105.5, 104.0]), "BUY", entry, lock, trail,
                            None, None, None) == ("stopped at lock", 5.0))

    # SELL is the mirror image and must close on the ASK.
    # asks are bid+0.10, so these are +5.4, +6.5, +5.5 in profit terms:
    # ratchets to 6.5-0.5 = 6.0, then +5.5 takes it out at exactly 6.00.
    sell = simulate_on_ticks(ticks([94.5, 93.4, 94.4]), "SELL", entry, lock, trail,
                             None, None, None)
    check("SELL mirrors the BUY case exactly", sell == ("trailed out", 6.0))

    # No ticks at all -> unresolved, never a fabricated profit.
    check("no tick history returns None rather than a number",
          simulate_on_ticks(ticks([]), "BUY", entry, lock, trail, None, None, None) is None)

    # Still open at the end of the window, with a later opposite cross.
    out = simulate_on_ticks(ticks([105.5, 106.0]), "BUY", entry, lock, trail, None,
                            datetime(2026, 9, 9, 1, tzinfo=timezone.utc), 108.0)
    check("unstopped run falls through to the opposite-cross exit",
          out == ("opposite cross", 8.0))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
