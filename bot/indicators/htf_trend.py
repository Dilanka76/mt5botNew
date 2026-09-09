"""Higher-timeframe trend as a column on the trading timeframe.

User's rule, 2026-09-09: on demo2_m3, when a cross confirms, look at the
M15 EMA13/21. If the trade agrees with that trend, aim for a bigger
take-profit; if not, use the normal one.

THE ONLY SUBTLE PART IS TIME. MT5 indexes a candle by the moment it
OPENED, so the M15 candle at 10:00 covers 10:00-10:15 and its close --
and therefore its EMAs -- do not exist until 10:15. Reading that row at
10:06 would be reading a candle that has not finished, which is
lookahead: the backtest would know how the next nine minutes turned out
and score a rule the live bot cannot run. Every value is therefore
shifted forward by one full higher-timeframe period before being mapped
onto the trading candles.

Before the first higher-timeframe candle closes the value is NaN, never
a guess. Callers must treat NaN as "no trend known" and fall back to
their default behaviour.
"""
from __future__ import annotations

import pandas as pd

from bot.config import EMAPeriodsConfig
from bot.indicators.ema import compute_emas

UPTREND = 1.0
DOWNTREND = -1.0


def compute_htf_trend(df: pd.DataFrame, htf_df: pd.DataFrame, htf_minutes: int,
                      periods: EMAPeriodsConfig) -> pd.DataFrame:
    """Returns a copy of `df` with an "htf_trend" column: +1 when the
    higher timeframe's EMA13 is above its EMA21 at the last CLOSED higher
    candle, -1 when below, NaN before the first one closes.

    `df` and `htf_df` must both be time-indexed and UTC-aware, oldest
    first, and `htf_df` must be the SAME symbol on the higher timeframe.
    """
    out = df.copy()
    if htf_df is None or htf_df.empty:
        out["htf_trend"] = float("nan")
        return out

    htf = compute_emas(htf_df, periods)
    trend = pd.Series(
        [UPTREND if a else DOWNTREND for a in (htf["ema13"] > htf["ema21"])],
        index=htf.index, dtype="float64",
    )
    # Index by CLOSE time, not open time -- see the module docstring.
    trend.index = trend.index + pd.Timedelta(minutes=htf_minutes)
    trend = trend[~trend.index.duplicated(keep="last")].sort_index()

    out["htf_trend"] = trend.reindex(out.index, method="ffill")
    return out


def agrees_with_trend(direction: str, htf_trend: float | None) -> bool:
    """True only when the trade runs WITH a known higher-timeframe trend.

    An unknown trend (NaN, or no column at all) is deliberately False:
    the bigger target is a bonus for confirmed alignment, so anything
    uncertain falls back to the normal one rather than guessing upward.
    """
    if htf_trend is None:
        return False
    try:
        value = float(htf_trend)
    except (TypeError, ValueError):
        return False
    if value != value:            # NaN
        return False
    return (value > 0) if direction == "BUY" else (value < 0)
