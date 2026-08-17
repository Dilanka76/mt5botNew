"""Reports real closed trades (from actual MT5 deal history, not a
backtest) for an account within a specific UTC time window — entry/exit
time, price, direction, volume, true realized profit (swap+commission on
both legs included, same as bot/analytics.py's trade_profit()).

    python scripts/inspect_live_trades.py --account demo1_m1 --from "2026-08-17 04:00" --to "2026-08-17 08:00"

Connects to MT5 only to read deal history, then disconnects — never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.analytics import trade_profit
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


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

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        deals = mt5.history_deals_get(dt_from, dt_to)
    finally:
        connector.disconnect()

    if not deals:
        print(f"No deals at all for {args.account} in this window.")
        return

    magic = config.execution.magic_number
    relevant = [d for d in deals if d.symbol == config.symbol and d.magic == magic]

    by_position: dict[int, list] = {}
    for d in relevant:
        by_position.setdefault(d.position_id, []).append(d)

    rows = []
    for position_id, deal_list in by_position.items():
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_OUT), None)
        if entry_deal is None or exit_deal is None:
            continue  # still open, or one leg fell outside this exact window
        rows.append({
            "position_id": position_id,
            # Use the ENTRY deal's own type directly — unambiguous, no
            # inversion logic needed (unlike inferring from exit_deal.type,
            # which requires remembering that a closing deal's type is the
            # OPPOSITE of the position being closed — easy to get backwards).
            "direction": "BUY" if entry_deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": exit_deal.volume,
            "entry_price": entry_deal.price,
            "exit_price": exit_deal.price,
            "open_time": datetime.fromtimestamp(entry_deal.time, tz=timezone.utc),
            "close_time": datetime.fromtimestamp(exit_deal.time, tz=timezone.utc),
            "profit": trade_profit(exit_deal, entry_deal),
            "comment": exit_deal.comment,
        })

    rows.sort(key=lambda r: r["open_time"])

    print(f"account={args.account} symbol={config.symbol} magic={magic} window={args.dt_from} to {args.dt_to}")
    print(f"{'open_time':<20} {'close_time':<20} {'dir':<5} {'lots':>6} {'entry':>10} {'exit':>10} {'profit':>10}  comment")
    total = 0.0
    wins = 0
    losses = 0
    for r in rows:
        total += r["profit"]
        if r["profit"] > 0:
            wins += 1
        elif r["profit"] < 0:
            losses += 1
        print(
            f"{r['open_time'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
            f"{r['close_time'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
            f"{r['direction']:<5} {r['volume']:>6.2f} {r['entry_price']:>10.2f} {r['exit_price']:>10.2f} "
            f"{r['profit']:>+10.2f}  {r['comment']}"
        )

    print(f"\n{len(rows)} closed trades in window. Wins={wins} Losses={losses} Total P/L={total:+.2f}")


if __name__ == "__main__":
    main()
