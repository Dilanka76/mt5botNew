"""Dumps the last N real, closed M1 candles for an account's symbol —
OHLC + EMA5/13/21, in both UTC and Colombo time, with a marker on any
candle where a cross was detected — so the user can scroll their own
MT5 chart to the same window and compare candle-by-candle, to find
exactly where (if anywhere) it diverges from what this bot sees.

    python scripts/dump_recent_candles.py --account demo1 --count 20

Also prints the exact connected account/server/symbol, so a chart
mismatch caused by a different account or a different symbol name
(e.g. XAUUSD vs XAUUSDp) is immediately obvious.

Read-only: only fetches historical OHLC, same as every other verify_*
script — never touches live/demo trading or places any order.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import detect_all_crosses

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--count", type=int, default=20, help="How many recent CLOSED candles to show (default 20).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        info = connector.account_info()
        print(f"Connected account: login={info.login}  server={info.server!r}  balance={info.balance:.2f}")
        print(f"Symbol: {config.symbol}   Timeframe: {config.timeframe}")
        print("(Check these three against what's actually open on your MT5 terminal — a mismatch here")
        print(" would explain everything: different account, different symbol name, or different chart period.)\n")

        # +2 for warm-up context (detect_all_crosses needs a candle before
        # the first one to classify its state) and to exclude the live/
        # forming candle (get_ohlc's last row), matching every other
        # script's convention in this project.
        df = get_ohlc(connector, config.symbol, config.timeframe, args.count + 2)
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    # get_ohlc() returns a NAIVE index (unlike get_ohlc_range(), which is
    # UTC-aware) — localize explicitly so every downstream comparison
    # (cross-marker matching, Colombo conversion) is consistently
    # UTC-aware, matching the rest of this project's convention.
    df.index = df.index.tz_localize("UTC")

    df = compute_emas(df, config.ema_periods)
    closed = df.iloc[:-1]  # drop the still-forming last candle, same convention as cross_detector.py
    events_by_time = {e.candle_time: e for e in detect_all_crosses(df)}

    shown = closed.iloc[-args.count:]
    print(f"{'Time (Colombo)':<26}{'Time (UTC)':<26}{'Open':>9}{'Close':>9}{'EMA5':>9}{'EMA13':>9}{'EMA21':>9}   Cross")
    print("-" * 108)
    for ts_utc, row in shown.iterrows():
        ts_local = ts_utc.astimezone(COLOMBO)
        marker = ""
        if ts_utc in events_by_time:
            marker = f"<-- {events_by_time[ts_utc].direction.value} cross"
        print(
            f"{ts_local.isoformat():<26}{ts_utc.isoformat():<26}"
            f"{row['open']:>9.2f}{row['close']:>9.2f}{row['ema5']:>9.2f}{row['ema13']:>9.2f}{row['ema21']:>9.2f}   {marker}"
        )

    print("\nScroll your MT5 chart to this same time window and compare the close prices and cross")
    print("points row by row. If the prices/times line up, we're looking at the same data — any strategy")
    print("difference is coming from the logic, not the source. If they DON'T line up, tell me exactly")
    print("which row differs and what your chart shows instead for that same candle.")


if __name__ == "__main__":
    main()
