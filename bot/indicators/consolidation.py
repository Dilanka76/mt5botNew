"""Is price trapped in a box on the higher timeframe?

User, 2026-09-20: "currently main problem is I cannot understand sideway
consolidation in the gold -- if I can identify it and miss that trade
that will help increase the win rate."

Every entry filter this project has tried and killed (ADX, candle colour,
tick volume, ATR bucket, efficiency ratio, EMA50/100) read ONE candle's
indicators. This reads the SHAPE of the last hour and a half on the
higher timeframe instead, in two numbers:

  cons_overlap   how much consecutive HTF candles cover the same prices.
                 Near 1.0 they print side by side and price is going
                 nowhere; near 0 each candle moves into new territory.
  cons_box_atr   the height of the window they made, in ATRs. A range
                 1.5 candles tall is a squeeze; 6 tall is a market
                 travelling.

A box needs BOTH: candles on top of each other AND a small range. Either
one alone is ordinary market behaviour.

NO HINDSIGHT. Both are computed from CLOSED higher-timeframe candles and
stamped at the moment that candle closed, then carried forward onto the
trading timeframe -- the same construction compute_htf_trend uses, for
the same reason. A row therefore only ever carries information that
existed before that row began.
"""
from __future__ import annotations

import pandas as pd

ATR_PERIOD = 14


def compute_consolidation(df: pd.DataFrame, htf_df: pd.DataFrame, htf_minutes: int,
                          lookback: int = 6) -> pd.DataFrame:
    """Returns a copy of `df` with "cons_overlap" and "cons_box_atr"
    columns, both read from the last CLOSED higher-timeframe candle, NaN
    until enough history exists.

    `df` and `htf_df` must be time-indexed, UTC-aware, oldest first, same
    symbol, `htf_df` on the higher timeframe.
    """
    out = df.copy()
    if htf_df is None or htf_df.empty or len(htf_df) < lookback + 2:
        out["cons_overlap"] = float("nan")
        out["cons_box_atr"] = float("nan")
        return out

    high, low, close = htf_df["high"], htf_df["low"], htf_df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat([high - low,
                            (high - prev_close).abs(),
                            (low - prev_close).abs()], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()

    # Overlap of each candle with the one before it: shared range over
    # combined range. 1.0 = identical range, 0.0 = no prices in common.
    shared = pd.concat([high, high.shift(1)], axis=1).min(axis=1) - \
        pd.concat([low, low.shift(1)], axis=1).max(axis=1)
    combined = pd.concat([high, high.shift(1)], axis=1).max(axis=1) - \
        pd.concat([low, low.shift(1)], axis=1).min(axis=1)
    pair_overlap = shared.clip(lower=0.0) / combined.replace(0.0, float("nan"))

    overlap = pair_overlap.rolling(lookback).mean()
    box = (high.rolling(lookback).max() - low.rolling(lookback).min()) / atr.replace(0.0, float("nan"))

    for name, series in (("cons_overlap", overlap), ("cons_box_atr", box)):
        stamped = series.copy()
        # Stamp at CLOSE time, not open time: the value is only known once
        # the candle has finished.
        stamped.index = stamped.index + pd.Timedelta(minutes=htf_minutes)
        stamped = stamped[~stamped.index.duplicated(keep="last")].sort_index()
        out[name] = stamped.reindex(out.index, method="ffill")
    return out


def is_consolidating(overlap: float | None, box_atr: float | None, filter_config) -> bool | None:
    """True = a box, skip. False = tradeable. None = cannot tell.

    None matters as much as the other two: a missing higher-timeframe
    candle, a fresh start with no history, or any NaN must leave the bot
    trading exactly as it does today. Failing OPEN is deliberate -- a
    filter that silently stops all trading because a number went missing
    is far worse than one that occasionally lets a bad trade through.
    """
    if filter_config is None or not filter_config.enabled:
        return None
    try:
        o, b = float(overlap), float(box_atr)
    except (TypeError, ValueError):
        return None
    if o != o or b != b:          # NaN
        return None
    return o >= filter_config.overlap_min and b <= filter_config.box_atr_max
