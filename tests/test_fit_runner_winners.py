"""Which historical trades would a runner have acted on?

Now measured at the ARM POINT ($5.00 here, a dollar under a $6.00
target), because that is where the runner starts acting -- sampling
from the target excluded every trade it damages.

    python3 tests/test_fit_runner_winners.py       (needs pandas)

scripts/fit_runner.py fits the TP-runner from history because demo1_m5
has only four real winning trades and the backtest cannot simulate a
runner at all. Everything downstream rests on this one function picking
the right trades: the runner only ever acts on a trade that REACHED its
target, so a trade wrongly counted as a winner invents profit that never
existed, and one wrongly excluded quietly shrinks the sample.

The ordering rule is the subtle part. A candle reports a high and a low
but not their order, so a candle that spans BOTH the stop and the target
is counted as stopped -- the pessimistic reading. That undercounts
winners rather than inventing them.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

import pandas as pd

from fit_runner import armed, replay

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def frame(rows: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """rows of (close, high, low, ema13, ema21)."""
    idx = pd.date_range("2026-09-09T00:00Z", periods=len(rows), freq="3min")
    return pd.DataFrame(
        {"close": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "open": [r[0] for r in rows],
         "ema13": [r[3] for r in rows], "ema21": [r[4] for r in rows]}, index=idx)


def ctx_from(rows):
    """rows of (high, low, close, above) -> the array context replay wants."""
    import numpy as np
    above = np.array([r[3] for r in rows], dtype=bool)
    changed = np.zeros(len(above), dtype=bool)
    changed[1:] = above[1:] != above[:-1]
    return {"high": np.array([r[0] for r in rows], dtype=float),
            "low": np.array([r[1] for r in rows], dtype=float),
            "close": np.array([r[2] for r in rows], dtype=float),
            "above": above, "changed": changed}




def check_replay() -> None:
    print("\nrunner vs no-runner, over identical candles")
    TP, BE = 6.0, 4.5
    entry = 100.0

    # Price climbs past the target and keeps going, then falls away.
    runs = ctx_from([(105.0, 104.5, 105.0, True), (106.5, 105.5, 106.0, True),
                     (109.0, 108.0, 108.5, True), (108.6, 103.0, 103.5, True)])
    off = replay(runs, 0, True, entry, tp=TP, lock_level=None, trail=0.0,
                 breakeven_lock=BE, max_candles=50, ratchet_first=False)
    on = replay(runs, 0, True, entry, tp=TP, lock_level=6.0, trail=0.5,
                breakeven_lock=BE, max_candles=50, ratchet_first=False)
    check("without the runner it closes at the target", off == ("take-profit", 6.0))
    check("with lock AT the target it holds and trails past it",
          on is not None and on[1] > 6.0)

    # It arms, wobbles, and never reaches the target. Both should land on
    # the breakeven -- the runner neither helps nor hurts here.
    stalls = ctx_from([(105.2, 105.0, 105.1, True), (105.9, 104.4, 104.5, True)])
    off2 = replay(stalls, 0, True, entry, tp=TP, lock_level=None, trail=0.0,
                  breakeven_lock=BE, max_candles=50, ratchet_first=False)
    on2 = replay(stalls, 0, True, entry, tp=TP, lock_level=6.0, trail=0.5,
                 breakeven_lock=BE, max_candles=50, ratchet_first=False)
    check("a trade that stalls before the target lands on the breakeven both ways",
          off2 == ("breakeven", BE) and on2 == ("breakeven", BE))

    # THE CASE THAT COSTS MONEY: lock BELOW the target. The stop sits at
    # $5.00 where price already is, so a dip ends it -- while the control,
    # with its take-profit still in place, rides on to $6.00.
    # Third candle needed: without a fall the trade is still open at the
    # end of the window and replay returns None, which is not the case
    # under test.
    dip = ctx_from([(105.3, 104.9, 105.0, True), (106.5, 106.0, 106.2, True),
                    (106.2, 104.0, 104.5, True)])
    off3 = replay(dip, 0, True, entry, tp=TP, lock_level=None, trail=0.0,
                  breakeven_lock=BE, max_candles=50, ratchet_first=False)
    on3 = replay(dip, 0, True, entry, tp=TP, lock_level=5.0, trail=0.5,
                 breakeven_lock=BE, max_candles=50, ratchet_first=False)
    check("locking BELOW the target can finish worse than the control",
          off3 == ("take-profit", 6.0) and on3 is not None and on3[1] < 6.0)
    check("...and locking AT the target cannot",
          replay(dip, 0, True, entry, tp=TP, lock_level=6.0, trail=0.5,
                 breakeven_lock=BE, max_candles=50, ratchet_first=False)[1] >= 6.0)



def main() -> None:
    print("finding the trades a runner would have acted on")

    # A BUY cross at row 1 (ema13 crosses above), entry 100.
    # Row 2 reaches +6.5 -> a winner. Row 4 crosses back.
    df = frame([
        (100, 100, 100, 1, 2),      # below
        (100, 100, 100, 2, 1),      # CROSS UP -> entry at close 100
        (104, 106.5, 103, 2, 1),    # reaches +6.5
        (105, 105, 104, 2, 1),
        (103, 103, 103, 1, 2),      # cross back
    ])
    w = armed(df, arm_point=5.0, stop=7.0)
    check("a BUY that reaches the ARM POINT is found", len(w) == 1)
    check("recorded at the candle that reached it, not the entry",
          w[0]["reached_at"] == df.index[2])
    check("entry price is the cross candle's CLOSE", w[0]["entry"] == 100)
    check("direction is right", w[0]["direction"] == "BUY")

    # Same shape but it never gets there.
    df2 = frame([
        (100, 100, 100, 1, 2),
        (100, 100, 100, 2, 1),
        (102, 103.0, 99, 2, 1),     # only +3
        (101, 101, 100, 2, 1),
        (99, 99, 99, 1, 2),
    ])
    check("a trade that never reaches the arm point is NOT sampled",
          armed(df2, arm_point=5.0, stop=7.0) == [])

    # Stopped first: low reaches -7 before any high reaches +6.
    df3 = frame([
        (100, 100, 100, 1, 2),
        (100, 100, 100, 2, 1),
        (94, 101, 92.5, 2, 1),      # -7.5 low, only +1 high
        (108, 108, 107, 2, 1),      # would have reached +8 later
        (99, 99, 99, 1, 2),
    ])
    check("stopped before arming -> not sampled", armed(df3, arm_point=5.0, stop=7.0) == [])
    check("with NO stop, the same trade DOES arm (demo2_m3's setup)",
          len(armed(df3, arm_point=5.0, stop=None)) == 1)

    # One candle spanning BOTH: pessimistic -> counts as stopped.
    df4 = frame([
        (100, 100, 100, 1, 2),
        (100, 100, 100, 2, 1),
        (100, 107, 92, 2, 1),       # +7 high AND -8 low in one candle
        (99, 99, 99, 1, 2),
    ])
    check("a candle spanning both stop and arm point counts as STOPPED",
          armed(df4, arm_point=5.0, stop=7.0) == [])

    # SELL mirrors.
    df5 = frame([
        (100, 100, 100, 2, 1),      # above
        (100, 100, 100, 1, 2),      # CROSS DOWN -> SELL entry 100
        (96, 101, 93.5, 1, 2),      # low 93.5 = +6.5 for a SELL
        (99, 99, 99, 2, 1),
    ])
    w5 = armed(df5, arm_point=5.0, stop=7.0)
    check("a SELL that reaches the arm point is found", len(w5) == 1 and w5[0]["direction"] == "SELL")

    # The entry candle itself must not be scanned -- entry is at its CLOSE,
    # so its own high/low already happened.
    df6 = frame([
        (100, 100, 100, 1, 2),
        (100, 108, 100, 2, 1),      # cross candle's own high is +8
        (100, 100, 100, 2, 1),
        (99, 99, 99, 1, 2),
    ])
    check("the entry candle's own range is not counted (entry is its close)",
          armed(df6, arm_point=5.0, stop=7.0) == [])



    check_replay()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
