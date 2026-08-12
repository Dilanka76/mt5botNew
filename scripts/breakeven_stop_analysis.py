"""Backtest-only analysis: for the strategy's actual historical trades
(entry logic completely unchanged — reads them from an existing
scripts/backtest.py run's .trades.jsonl), tests a hypothetical
breakeven-stop rule: once a trade has moved --trigger dollars in its
favor, an exit at the entry price is added as an extra condition,
independent of (and in addition to) the strategy's existing take-profit
and opposite-cross exits. Reports how many of the real losing trades
would have been intercepted at breakeven instead, and the resulting
change in win rate and total P/L.

Does NOT change any live strategy code or config, and does not
re-simulate entries — purely a what-if overlay on trades that already
happened (in the backtest), re-fetching only the underlying OHLC candle
data needed to reconstruct each trade's own intra-trade price path.

    python scripts/breakeven_stop_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11
    python scripts/breakeven_stop_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11 --trigger 3.0

Requires scripts/backtest.py to have already been run for that
account/date range (reads its .trades.jsonl sibling output).

Approximation, same caveats as the backtest engine itself: each candle's
own high/low stand in for two synthetic ticks (in candle-direction
order), not a true tick-by-tick replay. For an opposite-cross exit,
which in the real engine happens at that candle's own close (before any
of that candle's own tick range is evaluated), the final candle's own
tick pair is still scanned for a breakeven touch up to that point — a
reasonable, but not exact, approximation of "would breakeven have been
hit first" within that one candle.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.trade_stats import compute_day_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--trigger", type=float, default=2.0, help="Dollars of favorable price movement before the breakeven stop arms (default: 2.0)")
    return parser.parse_args()


def load_trades(account: str, date_from: str, date_to: str) -> list[dict]:
    path = PROJECT_ROOT / "reports" / "backtest" / account / f"{date_from}_{date_to}.trades.jsonl"
    if not path.exists():
        raise SystemExit(
            f"{path} not found — run scripts/backtest.py for this account/range first:\n"
            f"  python scripts/backtest.py --account {account} --from {date_from} --to {date_to}"
        )
    trades = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            trades.append(json.loads(line))
    return trades


def simulate_breakeven_exit(trade: dict, candles: pd.DataFrame, trigger: float) -> dict:
    """Returns a new trade dict with profit=0.0 and reason="breakeven_stop"
    if price ever returns to the entry price after having moved `trigger`
    dollars in the trade's favor, at any point between open and close.
    Otherwise returns the original trade dict unchanged (same object)."""
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    sign = 1 if direction == "BUY" else -1

    armed = False
    for _, candle in candles.iterrows():
        if candle["close"] >= candle["open"]:
            tick_sequence = (float(candle["low"]), float(candle["high"]))
        else:
            tick_sequence = (float(candle["high"]), float(candle["low"]))
        for price in tick_sequence:
            favorable = (price - entry_price) * sign
            if not armed and favorable >= trigger:
                armed = True
                continue  # the same tick that arms it can't also be the return-to-breakeven tick
            if armed and favorable <= 0:
                new_trade = dict(trade)
                new_trade["profit"] = 0.0
                new_trade["price"] = entry_price
                new_trade["reason"] = "breakeven_stop"
                return new_trade
    return trade


def main() -> None:
    args = parse_args()
    trades = load_trades(args.account, args.date_from, args.date_to)
    print(f"Loaded {len(trades)} trades from the existing {args.account} backtest ({args.date_from} to {args.date_to}).")
    print(f"Testing: breakeven stop arms after +${args.trigger:.2f} favorable movement.\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, date_from_dt, date_to_dt)
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    new_trades = []
    intercepted = []
    for t in trades:
        open_time = pd.Timestamp(t["open_time"])
        close_time = pd.Timestamp(t["close_time"])
        window = df.loc[open_time:close_time]
        new_t = simulate_breakeven_exit(t, window, args.trigger)
        new_trades.append(new_t)
        if new_t is not t and t["profit"] < 0:
            intercepted.append((t, new_t))

    old_summary = compute_day_stats(trades)
    new_summary = compute_day_stats(new_trades)

    print(f"{'':22}{'BEFORE':>14}{'AFTER':>14}")
    print(f"{'Total trades':22}{old_summary['total_trades']:>14}{new_summary['total_trades']:>14}")
    print(f"{'Wins':22}{old_summary['wins']:>14}{new_summary['wins']:>14}")
    print(f"{'Losses':22}{old_summary['losses']:>14}{new_summary['losses']:>14}")
    print(f"{'Breakeven (scratch)':22}{old_summary['breakeven']:>14}{new_summary['breakeven']:>14}")
    print(f"{'Win rate':22}{old_summary['win_rate']:>13.1f}%{new_summary['win_rate']:>13.1f}%")
    print(f"{'Total P/L':22}{old_summary['total_pl']:>14.2f}{new_summary['total_pl']:>14.2f}")
    print(f"{'Avg win':22}{old_summary['avg_win']:>14.2f}{new_summary['avg_win']:>14.2f}")
    print(f"{'Avg loss':22}{old_summary['avg_loss']:>14.2f}{new_summary['avg_loss']:>14.2f}")

    print(f"\n{len(intercepted)} losing trades would have been intercepted at breakeven "
          f"(recovered ${sum(-t['profit'] for t, _ in intercepted):.2f} of realized loss).")
    print("\nMethodology note: candle high/low stand in for two synthetic ticks (same approximation")
    print("as scripts/backtest.py), not a true tick-by-tick replay — see this script's own docstring.")


if __name__ == "__main__":
    main()
