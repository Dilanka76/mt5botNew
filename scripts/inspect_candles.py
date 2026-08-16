"""Prints raw OHLC + EMA13/21 (and EMA5/9 if configured) for a specific
time window, using the exact same data-fetching and EMA-computation code
path as the live bot and scripts/backtest.py — for manually verifying a
specific trade's entry/exit against real market data.

    python scripts/inspect_candles.py --account demo1_m1 --from "2026-08-10 07:30" --to "2026-08-10 09:00"

Connects to MT5 only to read historical candles, then disconnects —
never touches live/demo trading, same as scripts/backtest.py.
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = dt_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, dt_to)
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)
    window = df.loc[(df.index >= dt_from) & (df.index <= dt_to)]

    cols = [c for c in ("open", "high", "low", "close", "ema13", "ema21", "ema5", "ema9", "spread") if c in window.columns]
    with pd.option_context("display.max_rows", None, "display.width", 200, "display.float_format", "{:.2f}".format):
        print(f"account={args.account} symbol={config.symbol} timeframe={config.timeframe}")
        print(window[cols].to_string())


if __name__ == "__main__":
    main()
