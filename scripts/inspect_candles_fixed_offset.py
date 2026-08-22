"""One-off diagnostic: fetches real candle data for a historical window
using a MANUALLY SPECIFIED offset instead of measuring it live via
mt5_utc_offset(). Needed specifically because that live measurement
breaks when the market is currently closed (compares a stale last-tick
time against true "now", producing nonsense) -- see the 2026-08-22
weekend investigation. This bypasses that entirely by using the +3h
offset independently confirmed multiple times DURING 2026-08-21 itself
(while the market was open and the live measurement was reliable), not
today's broken live reading.

    python scripts/inspect_candles_fixed_offset.py --account demo1_m1 --from "2026-08-21 07:00" --to "2026-08-21 09:10" --offset-hours 3

Read-only: connects to MT5 only to read historical candles, never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5
import pandas as pd

from bot.config import load_config, validate_account_name
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

TIMEFRAME_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM", true UTC')
    parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM", true UTC')
    parser.add_argument("--offset-hours", type=float, required=True, help="Manually specified MT5-vs-true-UTC offset, e.g. 3")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    offset = timedelta(hours=args.offset_hours)

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = dt_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        connector.ensure_symbol(config.symbol)
        timeframe = connector.resolve_timeframe(config.timeframe)
        rates = mt5.copy_rates_range(config.symbol, timeframe, warmup_start + offset, dt_to + offset)
    finally:
        connector.disconnect()

    if rates is None or len(rates) == 0:
        print(f"No candle data returned: {mt5.last_error()}")
        return

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True) - offset
    df = df.drop_duplicates(subset="time").sort_values("time").set_index("time")

    df = compute_emas(df, config.ema_periods)
    window = df.loc[(df.index >= dt_from) & (df.index <= dt_to)]

    cols = [c for c in ("open", "high", "low", "close", "ema13", "ema21") if c in window.columns]
    with pd.option_context("display.max_rows", None, "display.width", 200, "display.float_format", "{:.2f}".format):
        print(f"account={args.account} symbol={config.symbol} timeframe={config.timeframe} offset={args.offset_hours}h (manually specified)")
        print(window[cols].to_string())

    if len(window):
        print(f"\nWindow high: {window['high'].max():.2f}   Window low: {window['low'].min():.2f}")


if __name__ == "__main__":
    main()
