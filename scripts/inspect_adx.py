"""Prints raw OHLC + EMA13/21 + ADX(14)/+DI/-DI for a specific time window,
using the same data-fetching path as scripts/inspect_candles.py — for
checking whether a real ADX trend-strength reading would have distinguished
a genuine reversal from market chop at a specific point in history (e.g. the
swapped_confirmed_reversal loss windows found in the real-trade report).

    python scripts/inspect_adx.py --account demo1_m1 --from "2026-08-19 22:50" --to "2026-08-19 23:17"

ADX(14), Wilder's original smoothing method (the standard definition used by
MT5's own built-in ADX indicator, so these numbers should match what you see
on the chart once you add the indicator there).

  ADX below ~20-25  = weak/no trend (ranging, choppy)
  ADX above ~25     = trending, real directional force

Connects to MT5 only to read historical candles, then disconnects — never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

TIMEFRAME_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
ADX_PERIOD = 14
# Wilder smoothing needs a long lead-in to stabilize; use well more than
# ADX_PERIOD candles of pre-window warmup regardless of the strategy's own
# (shorter) EMA warmup setting.
ADX_WARMUP_CANDLES = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    return parser.parse_args()


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.DataFrame:
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


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = dt_from - timedelta(minutes=ADX_WARMUP_CANDLES * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, dt_to)
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)
    df = compute_adx(df)
    window = df.loc[(df.index >= dt_from) & (df.index <= dt_to)]

    cols = [c for c in ("open", "high", "low", "close", "ema13", "ema21", "plus_di", "minus_di", "adx") if c in window.columns]
    with pd.option_context("display.max_rows", None, "display.width", 200, "display.float_format", "{:.2f}".format):
        print(f"account={args.account} symbol={config.symbol} timeframe={config.timeframe}")
        print(window[cols].to_string())

    valid_adx = window["adx"].dropna()
    if len(valid_adx):
        print(f"\nADX over this window: min={valid_adx.min():.1f} max={valid_adx.max():.1f} avg={valid_adx.mean():.1f}")
        print("(below ~20-25 = ranging/choppy, above ~25 = trending)")


if __name__ == "__main__":
    main()
