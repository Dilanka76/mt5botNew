"""Find, after the fact, the candle whose close triggered a given trade.

Every analysis script needs this, and until 2026-09-07 each carried its
own copy that was WRONG IN TWO WAYS. An audit
(scripts/audit_backtest_candle_matching.py) compared those copies against
the values the engines log at decision time and found they picked a
different candle on **238 of 245 real entries -- 97%** -- disagreeing
about the candle's own colour on 40-49% of trades. Every conclusion
drawn through them was computed on a candle the bot never looked at.

The two faults, and what this module does instead:

1. STATE vs STATE CHANGE. The old copies searched for a candle where
   EMA13 merely sat on the right side of EMA21. That matches any candle
   in an established trend. A trade is triggered by the candle where the
   two lines actually CROSSED -- `above != above.shift(1)` -- which is
   exactly the test the live engines use.

2. "CLOSED" vs "STARTED". The old copies accepted any candle with
   `index <= entry_time`. But a candle indexed 09:09 only *starts* at
   09:09 -- for a trade entered at 09:09:01 it was still forming, and the
   engine had actually acted on the 09:06 candle. A candle has closed
   only once `index + duration <= entry_time`, which is what is required
   here. Candle duration is inferred from the index itself, so callers
   do not have to pass a timeframe.

Returns None when no genuine cross is found in the lookback window --
deliberately. Dropping an unmatched trade is honest; silently pairing it
with the wrong candle is what caused the original problem. Callers
should count and report what they drop.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timedelta

import pandas as pd

DEFAULT_LOOKBACK = timedelta(minutes=30)

# Every unmatched trade is a trade dropped from whatever analysis is
# running. Dropping is the right call (see the module docstring), but a
# silent drop is how the original bug hid for weeks: if the picker only
# matches 60% of entries, the resulting numbers describe a biased
# subsample, not the strategy. So the rate is tracked and reported
# automatically at exit -- no caller has to remember to ask.
_matched = 0
_unmatched = 0


def match_stats() -> tuple[int, int]:
    """(matched, unmatched) lookups so far this process."""
    return _matched, _unmatched


@atexit.register
def _report_match_rate() -> None:
    total = _matched + _unmatched
    if not total:
        return
    rate = 100 * _matched / total
    print(f"\n[cross_lookup] matched {_matched}/{total} entries to a cross "
          f"candle ({rate:.0f}%).")
    if _unmatched:
        print(f"[cross_lookup] {_unmatched} entry/entries had no genuine "
              f"EMA13/21 cross in the lookback window and were EXCLUDED from "
              f"the numbers above -- treat the results as describing the "
              f"matched subset only.")


def find_cross_candle(
    df: pd.DataFrame,
    near: datetime,
    direction: str,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> pd.Timestamp | None:
    """The candle whose CLOSE triggered a trade in `direction` at `near`.

    `df` needs ema13/ema21 columns and a sorted DatetimeIndex.
    `direction` is "BUY" or "SELL".
    """
    global _matched, _unmatched

    if len(df) < 2:
        _unmatched += 1
        return None

    step = df.index.to_series().diff().median()
    if pd.isna(step) or step <= timedelta(0):
        _unmatched += 1
        return None

    # Only candles that had genuinely CLOSED by `near`.
    closed_by_entry = (df.index + step) <= near
    in_window = df.index >= (near - lookback)
    candidates = df.index[closed_by_entry & in_window]
    if len(candidates) == 0:
        _unmatched += 1
        return None

    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    # shift(1) is NaN on the first row, so `above != NaN` marks it as a
    # change and the very first candle of any fetched window would match
    # spuriously. It has no predecessor, so no cross can be observed there.
    changed.iloc[0] = False
    want_above = direction == "BUY"

    for idx in reversed(candidates):
        if bool(changed.loc[idx]) and bool(above.loc[idx]) == want_above:
            _matched += 1
            return idx
    _unmatched += 1
    return None
