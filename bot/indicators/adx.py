"""Average Directional Index (ADX) calculation — Wilder's original
smoothing method, matching MT5's own built-in ADX indicator's definition.

  ADX below ~20-25  = weak/no trend (ranging, choppy)
  ADX above ~25     = trending, real directional force

Extracted from scripts/inspect_adx.py (built 2026-08-20 to check real ADX
readings against real dual_cross_tight_exit_swap_confirm loss windows)
into a shared module so bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm_adx
and scripts/inspect_adx.py both use the exact same math — see that
engine's module docstring for why an ADX gate was added.
"""
from __future__ import annotations

import pandas as pd

DEFAULT_PERIOD = 14


def compute_adx(df: pd.DataFrame, period: int = DEFAULT_PERIOD) -> pd.DataFrame:
    """Returns a copy of `df` with plus_di/minus_di/adx columns added.

    `df` must be OHLC data (high/low/close columns) indexed oldest-first.
    Values are NaN for the first ~2*period rows while Wilder's smoothing
    warms up — callers need at least that many candles of history before
    the window they actually care about for ADX to be meaningful there.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
        result = series.copy()
        result.iloc[:period] = float("nan")
        first_val = series.iloc[:period].sum()
        result.iloc[period - 1] = first_val
        for i in range(period, len(series)):
            result.iloc[i] = result.iloc[i - 1] - (result.iloc[i - 1] / period) + series.iloc[i]
        return result

    tr_smooth = wilder_smooth(tr, period)
    plus_dm_smooth = wilder_smooth(plus_dm, period)
    minus_dm_smooth = wilder_smooth(minus_dm, period)

    plus_di = 100 * (plus_dm_smooth / tr_smooth)
    minus_di = 100 * (minus_dm_smooth / tr_smooth)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    adx = dx.copy()
    adx.iloc[:2 * period - 1] = float("nan")
    first_adx = dx.iloc[period - 1:2 * period - 1].mean()
    adx.iloc[2 * period - 2] = first_adx
    for i in range(2 * period - 1, len(dx)):
        adx.iloc[i] = (adx.iloc[i - 1] * (period - 1) + dx.iloc[i]) / period

    out = df.copy()
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    out["adx"] = adx
    return out
