"""Logic checks for bot/strategy/cross_lookup.find_cross_candle().

Run directly -- no pytest needed (it isn't installed on this project):

    python3 tests/test_cross_lookup.py

These exist because the two faults they pin down went unnoticed for
weeks and silently corrupted every entry-quality conclusion drawn in
this project (97% of real entries were matched to the wrong candle --
see the module docstring and scripts/audit_backtest_candle_matching.py).
Anyone tempted to "simplify" the picker should have to break a test to
do it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.strategy.cross_lookup import find_cross_candle


def make_df(start: str, freq_minutes: int, ema13: list[float], ema21: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=len(ema13), freq=f"{freq_minutes}min", tz=timezone.utc)
    return pd.DataFrame({"ema13": ema13, "ema21": ema21}, index=idx)


def check(label: str, got, want) -> None:
    assert got == want, f"{label}: got {got!r}, wanted {want!r}"
    print(f"  OK  {label}")


def main() -> None:
    print("cross_lookup logic checks")

    # M3 chart. EMA13 crosses above EMA21 on the 09:06 candle, which
    # closes at 09:09. A trade filled at 09:09:01 was triggered by THAT
    # candle -- not by the 09:09 candle, which had only just opened.
    df = make_df("2026-09-05 09:00", 3,
                 ema13=[1.0, 1.0, 3.0, 3.0, 3.0],   # 09:00 09:03 09:06 09:09 09:12
                 ema21=[2.0, 2.0, 2.0, 2.0, 2.0])
    entry = datetime(2026, 9, 5, 9, 9, 1, tzinfo=timezone.utc)
    check("entry 09:09:01 -> the candle that CLOSED (09:06), not the one still forming",
          find_cross_candle(df, entry, "BUY"), pd.Timestamp("2026-09-05 09:06", tz=timezone.utc))

    # Fault 1: a later entry mid-trend must still resolve to the CROSS
    # candle, not to whichever recent candle happens to sit on the right
    # side of the EMA (which is every candle in a trend).
    check("mid-trend entry still maps back to the cross candle",
          find_cross_candle(df, datetime(2026, 9, 5, 9, 15, 30, tzinfo=timezone.utc), "BUY"),
          pd.Timestamp("2026-09-05 09:06", tz=timezone.utc))

    # SELL is the mirror image.
    df_sell = make_df("2026-09-05 09:00", 3,
                      ema13=[3.0, 3.0, 1.0, 1.0, 1.0],
                      ema21=[2.0, 2.0, 2.0, 2.0, 2.0])
    check("SELL mirrored",
          find_cross_candle(df_sell, entry, "SELL"), pd.Timestamp("2026-09-05 09:06", tz=timezone.utc))

    # No genuine cross anywhere -> None. This is the case the shift(1)
    # first-row NaN used to break: row 0 has no predecessor, so it
    # registered as a "change" and matched spuriously.
    flat = make_df("2026-09-05 09:00", 3, ema13=[3.0] * 5, ema21=[2.0] * 5)
    check("no genuine cross -> None (first-row NaN artefact)",
          find_cross_candle(flat, entry, "BUY"), None)

    # A direction that never crossed must not borrow the other side's cross.
    check("wrong-direction cross is not reused",
          find_cross_candle(df, entry, "SELL"), None)

    # Fault 2 again, on M1: candle duration is inferred from the index,
    # so a 1-minute chart resolves to its own most recent closed cross.
    df_m1 = make_df("2026-09-05 09:00", 1,
                    ema13=[1.0, 1.0, 3.0, 3.0, 3.0],   # cross on 09:02, closes 09:03
                    ema21=[2.0, 2.0, 2.0, 2.0, 2.0])
    check("M1 chart -> candle duration inferred correctly",
          find_cross_candle(df_m1, datetime(2026, 9, 5, 9, 3, 1, tzinfo=timezone.utc), "BUY"),
          pd.Timestamp("2026-09-05 09:02", tz=timezone.utc))

    # A cross older than the lookback window is not matched -- better to
    # drop the trade than to pair it with a stale, unrelated cross.
    check("cross outside the lookback window -> None",
          find_cross_candle(df, datetime(2026, 9, 5, 11, 0, 0, tzinfo=timezone.utc), "BUY"), None)

    # Degenerate inputs must not raise.
    check("empty frame -> None",
          find_cross_candle(df.iloc[:0], entry, "BUY"), None)
    check("single-row frame (no step inferrable) -> None",
          find_cross_candle(df.iloc[:1], entry, "BUY"), None)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
