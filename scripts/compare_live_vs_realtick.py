"""Quantifies how often the live bot's once-per-second tick poll actually
misses a genuine entry signal, instead of guessing.

Runs the real-tick backtest (bot/backtest/runner.py with tick_provider=...,
which replays every single real historical tick — the "if the bot never
missed a tick" baseline) over [--from, --to], then fetches the account's
real closed-trade history from MT5 for the same window, and matches each
backtest-predicted entry (direction + time) against the nearest real live
entry of the same direction within --tolerance-seconds. Anything left
unmatched on the backtest side is a signal the strategy's own rules said
should have fired but the live process didn't act on — most likely a
poll-timing miss (see [[project-dual-cross-and-cross-confirmed]] for the
2026-08-17 case this script was built to quantify, not just anecdote).
Anything left unmatched on the live side is a live entry the backtest
doesn't explain — worth its own investigation, don't assume it's fine.

    python scripts/compare_live_vs_realtick.py --account demo1_m3 --from 2026-08-10 --to 2026-08-17

Connects to MT5 to pull OHLC, real ticks, and deal history, then
disconnects before the (offline) backtest replay runs. Never touches live
trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.backtest.runner import run_backtest
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest import TIMEFRAME_MINUTES, _fetch_real_ticks, _make_tick_provider  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--tolerance-seconds", type=float, default=180.0,
                         help="Max gap between a backtest-predicted entry time and a real live "
                              "entry time to count as the same signal (default 180s).")
    return parser.parse_args()


def _fetch_live_entries(connector: MT5Connector, config, dt_from: datetime, dt_to: datetime) -> list[dict]:
    deals = mt5.history_deals_get(dt_from, dt_to)
    if not deals:
        return []
    magic = config.execution.magic_number
    relevant = [d for d in deals if d.symbol == config.symbol and d.magic == magic and d.entry == mt5.DEAL_ENTRY_IN]
    return [
        {
            "direction": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
            "time": datetime.fromtimestamp(d.time, tz=timezone.utc),
            "price": d.price,
        }
        for d in relevant
    ]


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

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
        starting_balance = connector.account_info().balance

        print(f"Fetching real tick history for {args.date_from} to {args.date_to} ...")
        ticks_df = _fetch_real_ticks(connector, config.symbol, date_from, date_to)
        print(f"Total: {len(ticks_df)} real ticks loaded.")
        tick_provider = _make_tick_provider(ticks_df)

        print("Fetching real live entry history from MT5 deal log ...")
        live_entries = _fetch_live_entries(connector, config, date_from, date_to)
        print(f"Total: {len(live_entries)} real live entries in this window.")
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)
    bt_trades = run_backtest(config, df, date_from, contract_size, point, starting_balance, tick_provider=tick_provider)

    # Only tick-triggered entries are relevant here — on_new_candle-driven
    # entries (cross_confirmed, ema59_reentry) aren't subject to the
    # once-per-second poll-timing gap this script measures.
    bt_entries = [
        {"direction": t["direction"], "time": datetime.fromisoformat(t["open_time"]), "price": t["entry_price"]}
        for t in bt_trades
        if t["entry_type"] in ("tick_cross", "concurrent_tick_cross", "early_entry", "ema5_touch")
    ]
    bt_entries.sort(key=lambda e: e["time"])

    unmatched_live = list(live_entries)
    tolerance = timedelta(seconds=args.tolerance_seconds)
    caught: list[dict] = []
    missed: list[dict] = []

    for bt in bt_entries:
        best = None
        best_gap = None
        for live in unmatched_live:
            if live["direction"] != bt["direction"]:
                continue
            gap = abs((live["time"] - bt["time"]).total_seconds())
            if gap <= tolerance.total_seconds() and (best_gap is None or gap < best_gap):
                best, best_gap = live, gap
        if best is not None:
            caught.append({**bt, "live_time": best["time"], "gap_seconds": best_gap})
            unmatched_live.remove(best)
        else:
            missed.append(bt)

    total = len(bt_entries)
    n_caught = len(caught)
    n_missed = len(missed)
    miss_rate = (n_missed / total * 100) if total else 0.0

    print(f"\n=== Comparison: {args.account}, {args.date_from} to {args.date_to} ===")
    print(f"Real-tick backtest predicted {total} tick-triggered entries (the 'every tick seen' baseline).")
    print(f"Live actually caught:  {n_caught} ({100 - miss_rate:.1f}%)")
    print(f"Live likely missed:    {n_missed} ({miss_rate:.1f}%)")
    print(f"Live entries with no backtest match (unexplained, needs its own look): {len(unmatched_live)}")

    if missed:
        print("\n--- Missed entries (backtest said yes, no matching live trade found) ---")
        for m in missed:
            print(f"  {m['time'].isoformat()}  {m['direction']:<4}  predicted entry price {m['price']:.2f}")

    if unmatched_live:
        print("\n--- Live entries with no backtest counterpart ---")
        for u in unmatched_live:
            print(f"  {u['time'].isoformat()}  {u['direction']:<4}  live entry price {u['price']:.2f}")

    if caught:
        gaps = [c["gap_seconds"] for c in caught]
        print(f"\nMatched-entry timing gap: avg {sum(gaps)/len(gaps):.1f}s, max {max(gaps):.1f}s "
              f"(how close live execution landed to the real qualifying tick)")


if __name__ == "__main__":
    main()
