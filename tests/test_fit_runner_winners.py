"""Which historical trades would a runner have acted on?

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

from fit_runner import winners

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
    w = winners(df, tp=6.0, stop=7.0)
    check("a BUY that reaches the target is found", len(w) == 1)
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
    check("a trade that never reaches the target is NOT a winner",
          winners(df2, tp=6.0, stop=7.0) == [])

    # Stopped first: low reaches -7 before any high reaches +6.
    df3 = frame([
        (100, 100, 100, 1, 2),
        (100, 100, 100, 2, 1),
        (94, 101, 92.5, 2, 1),      # -7.5 low, only +1 high
        (108, 108, 107, 2, 1),      # would have reached +8 later
        (99, 99, 99, 1, 2),
    ])
    check("stopped before the target -> not a winner", winners(df3, tp=6.0, stop=7.0) == [])
    check("with NO stop, the same trade IS a winner (demo2_m3's setup)",
          len(winners(df3, tp=6.0, stop=None)) == 1)

    # One candle spanning BOTH: pessimistic -> counts as stopped.
    df4 = frame([
        (100, 100, 100, 1, 2),
        (100, 100, 100, 2, 1),
        (100, 107, 92, 2, 1),       # +7 high AND -8 low in one candle
        (99, 99, 99, 1, 2),
    ])
    check("a candle spanning both stop and target counts as STOPPED",
          winners(df4, tp=6.0, stop=7.0) == [])

    # SELL mirrors.
    df5 = frame([
        (100, 100, 100, 2, 1),      # above
        (100, 100, 100, 1, 2),      # CROSS DOWN -> SELL entry 100
        (96, 101, 93.5, 1, 2),      # low 93.5 = +6.5 for a SELL
        (99, 99, 99, 2, 1),
    ])
    w5 = winners(df5, tp=6.0, stop=7.0)
    check("a SELL that reaches the target is found", len(w5) == 1 and w5[0]["direction"] == "SELL")

    # The entry candle itself must not be scanned -- entry is at its CLOSE,
    # so its own high/low already happened.
    df6 = frame([
        (100, 100, 100, 1, 2),
        (100, 108, 100, 2, 1),      # cross candle's own high is +8
        (100, 100, 100, 2, 1),
        (99, 99, 99, 1, 2),
    ])
    check("the entry candle's own range is not counted (entry is its close)",
          winners(df6, tp=6.0, stop=7.0) == [])

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
