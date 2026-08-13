"""Checks how far back this MT5 terminal's LOCALLY CACHED candle history
actually goes for a symbol/timeframe — the same question that silently
tripped up an earlier backtest (a 2026-02-01 to 2026-08-11 request
returned ~0 candles per month before ~2026-05-01, discovered only by
noticing "1 candle per chunk" in the output).

This is a terminal-cache question, not a broker-retention question: most
brokers keep years of server-side history, but a desktop MT5 terminal
only caches what it has actually been asked to display/download — a
freshly connected terminal (or one that's never had its XAUUSDp M1 chart
scrolled back) can report far less than the broker actually has. This
script tells you where today's actual cutoff is; it does NOT extend the
cache itself (see the printed note at the end for how to do that).

Read-only: only calls mt5.copy_rates_range for small probes, one calendar
month at a time going backward from today — never touches the live/demo
trading connection or places any order (same connector pattern as
scripts/verify_candle_utc.py and scripts/backtest.py).

    python scripts/check_history_range.py --account demo1
    python scripts/check_history_range.py --account demo1 --months-back 24
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

import MetaTrader5 as mt5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument(
        "--months-back", type=int, default=18,
        help="How many calendar months to probe backward from today (default 18).",
    )
    return parser.parse_args()


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_month(dt: datetime) -> datetime:
    return (dt.replace(day=28) + timedelta(days=4)).replace(day=1)


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()

    try:
        info = connector.account_info()
        print(f"Connected to MT5 as: login={info.login} server={info.server!r} balance={info.balance:.2f}")
        print(f"Checking {config.symbol} {config.timeframe} cached history, {args.months_back} months back from today.")
        print()

        connector.ensure_symbol(config.symbol)
        timeframe = connector.resolve_timeframe(config.timeframe)

        now = datetime.now(timezone.utc)
        cursor = month_start(now)
        rows = []
        for _ in range(args.months_back):
            chunk_start = cursor
            chunk_end = min(add_month(cursor), now)
            rates = mt5.copy_rates_range(config.symbol, timeframe, chunk_start, chunk_end)
            count = 0 if rates is None else len(rates)
            rows.append((chunk_start, chunk_end, count))
            cursor = month_start(cursor - timedelta(days=1))  # step back one calendar month

        print(f"{'month':<10}{'candles':>10}   status")
        print("-" * 45)
        earliest_real_month = None
        for chunk_start, chunk_end, count in rows:
            # A real, fully-cached month of M1 data has tens of thousands
            # of candles (~43,000 if every minute traded nonstop); a
            # handful of stray candles (as seen in the original gap
            # discovery) means "essentially no real history," not "a
            # quiet month" — real months are never that sparse.
            status = "real data" if count > 1000 else ("EMPTY / no cache" if count == 0 else "SPARSE — likely cache edge, not real")
            print(f"{chunk_start.strftime('%Y-%m'):<10}{count:>10}   {status}")
            if count > 1000:
                earliest_real_month = chunk_start

        print()
        if earliest_real_month is None:
            print("No month in the probed range had substantial cached data — widen --months-back.")
        else:
            print(f"Earliest month with substantial real cached data: {earliest_real_month.strftime('%Y-%m')}")
            print(f"Usable backtest range right now: roughly {earliest_real_month.date()} to {now.date()} "
                  f"(~{(now - earliest_real_month).days} days).")
        print()
        print("This reflects the TERMINAL's local cache, not necessarily the broker's actual retention limit.")
        print("To extend it: open this account's MT5 terminal GUI (RDP), open an XAUUSDp M1 chart, then either")
        print("press Home repeatedly / scroll the chart far left to force older data to load, or open the")
        print("History Center (F2), select Symbol=XAUUSDp Period=M1, and use its Download button for an")
        print("explicit older date range. Re-run this script afterward to confirm the cutoff actually moved.")

    finally:
        connector.disconnect()  # read-only fetch — never touches the live trading connection


if __name__ == "__main__":
    main()