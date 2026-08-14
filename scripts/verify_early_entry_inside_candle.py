"""Proves, against real historical data, that an early_entry trade's
entry price genuinely lands INSIDE the cross candle's own high/low
range (a real, forming price from before that candle closed) — not
equal to that candle's close (which would mean it's secretly the same
as an "immediate" entry) and not from some other, later candle.

For each early_entry trade, looks up the candle at its own open_time
(the same candle bot/backtest/runner.py's PHASE 1 tick-simulated) and
prints that candle's open/high/low/close next to the trade's actual
entry_price, so you can see directly that low <= entry_price <= high
and entry_price != close.

    python scripts/verify_early_entry_inside_candle.py --account demo1 --from 2026-05-01 --to 2026-08-13 --threshold 0.10 --limit 15

Connects to MT5 only to pull historical candles + the symbol's contract
size/point/current balance, then disconnects before the (offline)
replay begins — never touches the live/demo trading connection or
places any order.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.backtest.runner import run_backtest
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--threshold", type=float, default=0.10, help="early_entry_threshold_usd to test (default 0.10)")
    parser.add_argument("--limit", type=int, default=15, help="Max early_entry trades to print (default 15)")
    parser.add_argument("--balance", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    setup_logging(config.logging, f"{args.account}-backtest")

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if date_to <= date_from:
        raise ValueError("--to must be after --from")

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to)
        symbol_info = connector.symbol_info(config.symbol)
        contract_size = symbol_info.trade_contract_size
        point = symbol_info.point
        starting_balance = args.balance if args.balance is not None else connector.account_info().balance
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)

    variant_config = replace(config, early_entry_threshold_usd=args.threshold)
    trades = run_backtest(variant_config, df, date_from, contract_size, point, starting_balance)

    early_trades = [t for t in trades if t["entry_type"] == "early_entry"][: args.limit]

    print(f"Account: {args.account}   threshold: ${args.threshold:g}   point: {point}   "
          f"early_entry trades found: {len([t for t in trades if t['entry_type'] == 'early_entry'])} "
          f"(showing first {len(early_trades)})")
    print()
    print(
        "BUY fills at the ASK (bid + that candle's own spread), but MT5's OHLC is bid-based —\n"
        "so a BUY entry landing slightly ABOVE the raw 'candle high' by exactly that candle's own\n"
        "spread is CORRECT execution modeling, not evidence of a wrong candle. 'inside (spread-adj)'\n"
        "checks against [low, high + spread] for BUY and [low, high] for SELL — the real fillable range."
    )
    print()
    print(f"{'open_time (UTC)':<22}{'dir':<6}{'low':>10}{'high':>10}{'close':>10}"
          f"{'spread($)':>10}{'entry_price':>13}{'inside (raw)':>13}{'inside (spread-adj)':>21}{'!= close?':>11}")
    print("-" * 140)

    for t in early_trades:
        candle_time = pd.Timestamp(t["open_time"])
        candle = df.loc[candle_time]
        spread_price = float(candle["spread"]) * point
        raw_inside = candle["low"] <= t["entry_price"] <= candle["high"]
        adj_high = candle["high"] + spread_price if t["direction"] == "BUY" else candle["high"]
        adj_inside = candle["low"] <= t["entry_price"] <= adj_high
        not_close = abs(t["entry_price"] - candle["close"]) > 1e-9
        print(
            f"{str(candle_time):<22}{t['direction']:<6}{candle['low']:>10.2f}{candle['high']:>10.2f}"
            f"{candle['close']:>10.2f}{spread_price:>10.2f}{t['entry_price']:>13.2f}"
            f"{'YES' if raw_inside else 'NO':>13}{'YES' if adj_inside else 'NO':>21}"
            f"{'YES' if not_close else 'NO':>11}"
        )

    if not early_trades:
        print("(no early_entry trades in this range at this threshold — try a larger --threshold or wider date range)")
        return

    def _adj_inside(t):
        candle_time = pd.Timestamp(t["open_time"])
        candle = df.loc[candle_time]
        spread_price = float(candle["spread"]) * point
        adj_high = candle["high"] + spread_price if t["direction"] == "BUY" else candle["high"]
        return candle["low"] <= t["entry_price"] <= adj_high

    all_inside_adj = all(_adj_inside(t) for t in early_trades)
    print()
    print(f"All {len(early_trades)} shown entries landed inside their own candle's real fillable (spread-adjusted) range: {all_inside_adj}")


if __name__ == "__main__":
    main()
