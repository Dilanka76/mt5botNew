"""Prints MT5's reported time alongside the Windows machine's own clock
and true UTC, so real timestamps (decisions.jsonl, trade reports) can be
verified against what MT5 itself and the server actually think "now" is.

    python scripts/check_mt5_time.py --account demo1_m1

Connects to MT5 only to read the latest tick's timestamp, then
disconnects — never touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        tick = connector.get_tick(config.symbol)
        account_info = connector.account_info()
    finally:
        connector.disconnect()

    print(f"account={args.account} symbol={config.symbol}")
    print(f"MT5 latest tick time (raw epoch, treated as UTC): {datetime.fromtimestamp(tick.time, tz=timezone.utc)}")
    print(f"Windows machine's own clock (local):              {datetime.now()}")
    print(f"True UTC right now (this process's own clock):    {datetime.now(timezone.utc)}")
    print(f"\nAccount server: {config.mt5.server or '(from running terminal)'}  balance={account_info.balance:.2f}")
    print(
        "\nIf the MT5 tick time differs from true UTC by a consistent offset (e.g. exactly "
        "2 or 3 hours), that's the broker's server-time convention — decisions.jsonl timestamps "
        "are always true UTC (Python's own clock), while MT5 deal/position .time fields use "
        "this same broker-server convention, NOT true UTC."
    )


if __name__ == "__main__":
    main()
