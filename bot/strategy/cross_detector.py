"""Detects EMA13/EMA21 crosses, confirmed only on a closed candle.

Important: bot.data.market_data.get_ohlc() pulls bars via MT5's
copy_rates_from_pos(symbol, timeframe, 0, count) convention, where position 0
is the CURRENTLY FORMING (incomplete) bar. Rows come back oldest-to-newest,
so df.iloc[-1] is that live, still-changing bar and df.iloc[-2] is the most
recent fully CLOSED bar. Per the locked-in decision to confirm crosses only
on candle close (avoids whipsaw from a cross that reverses intrabar), a cross
event compares df.iloc[-3] (closed) against df.iloc[-2] (closed) and
deliberately ignores df.iloc[-1].
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Direction(Enum):
    BUY = "BUY"   # EMA13 crossed above EMA21 (bullish)
    SELL = "SELL"  # EMA13 crossed below EMA21 (bearish)


class CrossState(Enum):
    ABOVE = "ABOVE"  # ema13 > ema21
    BELOW = "BELOW"  # ema13 < ema21


@dataclass
class CrossEvent:
    direction: Direction
    candle_time: pd.Timestamp
    close: float
    ema13: float
    ema21: float


def _classify(row: pd.Series) -> CrossState | None:
    if row["ema13"] > row["ema21"]:
        return CrossState.ABOVE
    if row["ema13"] < row["ema21"]:
        return CrossState.BELOW
    return None  # exactly equal — treat as indeterminate, extremely rare with float prices


def detect_cross(df: pd.DataFrame) -> CrossEvent | None:
    """Checks whether the two most recent CLOSED candles show a fresh EMA13/21 cross.

    `df` must already have ema13/ema21 columns (see bot.indicators.ema.compute_emas)
    and at least 3 rows (the live bar + 2 closed bars). Returns None if there's
    no data, no state change, or either candle's EMA relationship is indeterminate.
    """
    if len(df) < 3:
        return None

    prev_closed = df.iloc[-3]
    last_closed = df.iloc[-2]

    prev_state = _classify(prev_closed)
    curr_state = _classify(last_closed)

    if prev_state is None or curr_state is None or prev_state == curr_state:
        return None

    direction = Direction.BUY if curr_state == CrossState.ABOVE else Direction.SELL

    return CrossEvent(
        direction=direction,
        candle_time=last_closed.name,
        close=last_closed["close"],
        ema13=last_closed["ema13"],
        ema21=last_closed["ema21"],
    )


def detect_all_crosses(df: pd.DataFrame) -> list[CrossEvent]:
    """Scans every closed candle in `df` (i.e. all rows except the live last
    one) and returns every EMA13/21 cross found, oldest first. Used for
    verifying historical cross detection against a chart, and for backtesting
    later — detect_cross() above is the live, single-latest-cross version of
    the same logic used in the real trading loop.
    """
    closed = df.iloc[:-1]  # drop the live/forming last candle
    events: list[CrossEvent] = []
    prev_state: CrossState | None = None

    for _, row in closed.iterrows():
        state = _classify(row)
        if state is not None and prev_state is not None and state != prev_state:
            direction = Direction.BUY if state == CrossState.ABOVE else Direction.SELL
            events.append(
                CrossEvent(
                    direction=direction,
                    candle_time=row.name,
                    close=row["close"],
                    ema13=row["ema13"],
                    ema21=row["ema21"],
                )
            )
        if state is not None:
            prev_state = state

    return events


def calculate_gap(event: CrossEvent) -> float:
    """Distance between the cross candle's close and EMA13, mirrored by direction.

    BUY (bullish cross):  gap = close - ema13   (positive when price is above EMA13)
    SELL (bearish cross): gap = ema13 - close   (positive when price is below EMA13)

    Spec: gap < gap_threshold_usd -> enter immediately; otherwise wait for
    price to touch EMA5 before entering.
    """
    if event.direction == Direction.BUY:
        return event.close - event.ema13
    return event.ema13 - event.close
