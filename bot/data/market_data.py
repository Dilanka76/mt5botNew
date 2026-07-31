"""Pulls OHLC candle data from MT5 into pandas DataFrames."""
from __future__ import annotations

import pandas as pd
import MetaTrader5 as mt5

from bot.mt5_connector import MT5Connector


def get_ohlc(connector: MT5Connector, symbol: str, timeframe_str: str, count: int) -> pd.DataFrame:
    """Fetch the most recent `count` candles for `symbol`.

    Per MT5's copy_rates_from_pos convention, the LAST row (iloc[-1]) is the
    currently forming, still-incomplete candle; iloc[-2] is the most recent
    fully closed candle. Callers that need confirmed/closed-only data (e.g.
    bot.strategy.cross_detector) must account for this themselves.

    Returns a DataFrame indexed by time with columns:
    open, high, low, close, tick_volume, spread, real_volume
    """
    connector.ensure_symbol(symbol)
    timeframe = connector.resolve_timeframe(timeframe_str)

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No candle data returned for {symbol} {timeframe_str}: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    return df
