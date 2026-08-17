"""Reports this account's currently OPEN positions (from real MT5
positions_get(), not closed deal history) — direction, entry time/price,
current price, floating P/L, ticket.

    python scripts/inspect_open_positions.py --account demo1_m1

Connects to MT5 only to read positions, then disconnects — never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

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
        positions = mt5.positions_get(symbol=config.symbol)
    finally:
        connector.disconnect()

    magic = config.execution.magic_number
    relevant = [p for p in (positions or []) if p.magic == magic]

    if not relevant:
        print(f"No open positions for {args.account} (symbol={config.symbol}, magic={magic}).")
        return

    print(f"account={args.account} symbol={config.symbol} magic={magic}")
    print(f"{'ticket':>10}  {'dir':<5} {'volume':>6} {'open_time':<20} {'entry':>10} {'current':>10} {'sl':>10} {'tp':>10} {'profit':>10}  comment")
    for p in sorted(relevant, key=lambda x: x.time):
        direction = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        open_time = datetime.fromtimestamp(p.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{p.ticket:>10}  {direction:<5} {p.volume:>6.2f} {open_time:<20} {p.price_open:>10.2f} "
            f"{p.price_current:>10.2f} {p.sl:>10.2f} {p.tp:>10.2f} {p.profit:>+10.2f}  {p.comment}"
        )

    total_profit = sum(p.profit for p in relevant)
    print(f"\n{len(relevant)} open position(s). Floating P/L total: {total_profit:+.2f}")


if __name__ == "__main__":
    main()
