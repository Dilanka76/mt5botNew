"""Candle duration in minutes, in ONE place.

Five scripts each carried their own copy of this table and three of them
predated the M3 accounts, so they simply crashed with KeyError: 'M3'
(scripts/stop_loss_sweep_analysis.py, 2026-09-08). That is the same
drift-between-copies problem that produced the wrong-candle bug -- see
bot/strategy/cross_lookup.py -- so it gets the same treatment.

Must stay in step with bot/mt5_connector.py's TIMEFRAME map, which is
the set of timeframes this project can actually fetch. Kept separate
from it deliberately: that module imports MetaTrader5, which has
Windows-only wheels, and this table needs to be readable from anywhere.
"""
from __future__ import annotations

TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M3": 3,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def minutes_for(timeframe: str) -> int:
    """Minutes per candle, with an error that says what to do about it."""
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except KeyError:
        raise KeyError(
            f"Unknown timeframe {timeframe!r}. Add it to bot/timeframes.py "
            f"(known: {', '.join(TIMEFRAME_MINUTES)})."
        ) from None
