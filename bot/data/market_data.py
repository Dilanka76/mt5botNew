"""Pulls OHLC candle data from MT5 into pandas DataFrames."""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import MetaTrader5 as mt5

from bot.mt5_connector import MT5Connector

logger = logging.getLogger("bot.data.market_data")


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


def get_ohlc_range(
    connector: MT5Connector, symbol: str, timeframe_str: str, date_from: datetime, date_to: datetime,
) -> pd.DataFrame:
    """Fetch every candle for `symbol` between `date_from` and `date_to`
    (inclusive, both must be UTC-aware) — for backtesting, where get_ohlc's
    "most recent N candles" isn't enough.

    Fetched in monthly chunks rather than one call: MT5's copy_rates_range
    silently returns fewer bars than requested (never an error) when a
    range exceeds what the broker/terminal actually has cached, so a
    single big call can hide a real historical-data gap. Chunking makes
    each month's actual row count visible in the log, so a gap shows up
    immediately instead of as a mysteriously low trade count later.

    Returns the same shape as get_ohlc: time-indexed (UTC-aware, unlike
    get_ohlc's naive index — session-window gating needs a real timezone
    to convert from), open/high/low/close/tick_volume/spread/real_volume.
    """
    connector.ensure_symbol(symbol)
    timeframe = connector.resolve_timeframe(timeframe_str)

    chunks: list[pd.DataFrame] = []
    chunk_start = date_from
    while chunk_start < date_to:
        # first day of the next month, capped at date_to
        if chunk_start.month == 12:
            next_month_start = chunk_start.replace(year=chunk_start.year + 1, month=1, day=1)
        else:
            next_month_start = chunk_start.replace(month=chunk_start.month + 1, day=1)
        chunk_end = min(next_month_start, date_to)

        rates = mt5.copy_rates_range(symbol, timeframe, chunk_start, chunk_end)
        row_count = len(rates) if rates is not None else 0
        logger.info(
            "get_ohlc_range: %s..%s -> %d candles",
            chunk_start.date(), chunk_end.date(), row_count,
        )
        if row_count == 0:
            logger.warning(
                "get_ohlc_range: zero candles for %s..%s — likely a historical-data gap "
                "(broker hasn't cached this range), not necessarily a real market closure",
                chunk_start.date(), chunk_end.date(),
            )
        else:
            chunks.append(pd.DataFrame(rates))

        chunk_start = chunk_end

    if not chunks:
        raise RuntimeError(f"No candle data returned for {symbol} {timeframe_str} {date_from}..{date_to}: {mt5.last_error()}")

    df = pd.concat(chunks, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop_duplicates(subset="time").sort_values("time")
    df.set_index("time", inplace=True)
    return df
