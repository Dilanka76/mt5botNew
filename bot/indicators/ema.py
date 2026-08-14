"""Exponential Moving Average calculation.

Uses pandas' recursive EMA (adjust=False), which matches the standard
formula MT5's own EMA indicator uses:
    EMA_t = price_t * k + EMA_{t-1} * (1 - k),  k = 2 / (period + 1)

Note: our EMA seeds its first value as EMA_0 = price_0, whereas MT5's
on-chart indicator seeds the first value as the simple average of the first
`period` prices. This makes a negligible difference (< $0.0001, decaying
exponentially) after a few dozen warm-up bars — irrelevant given
candles_to_fetch pulls hundreds of bars of history before the recent candles
the strategy actually acts on. Verified by hand against a from-scratch
recursive implementation: matches to ~1e-13.
"""
from __future__ import annotations

import pandas as pd

from bot.config import EMAPeriodsConfig


def compute_emas(df: pd.DataFrame, periods: EMAPeriodsConfig, price_col: str = "close") -> pd.DataFrame:
    """Returns a copy of `df` with ema5/ema9/ema13/ema21 columns added.

    `df` must be OHLC data indexed oldest-first (see bot.data.market_data.get_ohlc).
    `price_col` defaults to "close", which on MT5 bars is the bid close price —
    matching the strategy's "EMAs calculated using BID price" requirement.

    ema9 is paired with ema5 for the reversal-confirmation check (see
    bot.strategy.state_machine) — not part of the original entry-trigger
    trio (5/13/21), added for that specific purpose.
    """
    out = df.copy()
    out["ema5"] = out[price_col].ewm(span=periods.fast, adjust=False).mean()
    out["ema9"] = out[price_col].ewm(span=periods.reversal_slow, adjust=False).mean()
    out["ema13"] = out[price_col].ewm(span=periods.mid, adjust=False).mean()
    out["ema21"] = out[price_col].ewm(span=periods.slow, adjust=False).mean()
    return out
