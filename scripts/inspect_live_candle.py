"""One-off diagnostic: shows the CURRENTLY FORMING candle plus the last
few closed ones, exactly as the live bot itself sees them each loop
iteration (same get_ohlc() position-based fetch main.py uses -- the
forming candle is included at the end, unlike inspect_candles_fixed_offset.py's
copy_rates_range which only returns finalized bars).

Prints MT5 app-local time (true UTC + the account's configured/measured
offset) alongside true UTC, so it's directly comparable to what's shown
on the MT5 app.

    python scripts/inspect_live_candle.py --account demo1_m1

Read-only: connects to MT5 only to read live candles, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.analytics import mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc
from bot.indicators.adx import compute_adx
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--rows", type=int, default=5, help="How many recent candles to show (default 5)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc(connector, config.symbol, config.timeframe, config.candles_to_fetch)
        offset = mt5_utc_offset(connector, config.symbol)
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)
    df = compute_adx(df, period=config.swap_adx_filter.adx_period if config.swap_adx_filter else 14)

    tail = df.tail(args.rows).copy()
    tail["true_utc"] = tail.index - offset
    tail["app_time"] = tail.index

    cols = ["app_time", "true_utc", "open", "high", "low", "close", "ema13", "ema21", "adx"]
    with pd.option_context("display.max_rows", None, "display.width", 220, "display.float_format", "{:.2f}".format):
        print(f"account={args.account} symbol={config.symbol} timeframe={config.timeframe} "
              f"measured_offset={offset.total_seconds() / 3600:.2f}h")
        print(tail[cols].to_string(index=False))
        print(
            f"\nLast row above (app_time={tail['app_time'].iloc[-1]}) is the CURRENTLY FORMING candle "
            f"-- still changing, not yet closed. The row before it is the last CLOSED candle -- "
            f"that's the one the bot actually bases decisions on."
        )


if __name__ == "__main__":
    main()
