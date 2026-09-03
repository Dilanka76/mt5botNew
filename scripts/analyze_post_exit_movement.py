"""Research script (read-only, no live behavior change): after each trade
CLOSES, how much further does price keep moving in that trade's
direction? I.e. how much money is each exit mechanism leaving on the
table -- or saving us from?

Motivation (2026-09-03): every piece of research in this project so far
has been about which trades to ENTER (the "7 points", trend filter,
clustering, etc. -- see project_demo3_entryfilter_research and
project_trend_filter_research). The exits have never been examined, yet
the real numbers keep pointing at them: three of four accounts are
profitable despite sitting BELOW their nominal risk-reward breakeven win
rate (demo1_m1 48.9% vs 50% needed at $5/$5; demo2_m3 58.5% vs 62.5%
needed at $10/$6), which is only possible because realized losses are
smaller than the nominal stop -- i.e. the exit mechanisms (breakeven
stop, swap reversal) are carrying the strategy.

For every closed trade, replays REAL candles after the exit and measures:
  - post-exit FAVORABLE movement: how much further price went in the
    trade's direction (for a take_profit exit, this is money left on the
    table; for a stop_loss exit, this is how badly the stop was timed).
  - post-exit ADVERSE movement: how much price went against the trade's
    direction after exit (for a swap/breakeven exit, this is loss the
    exit SAVED us from).

Broken down by the engine's own close category (joined from
decisions.jsonl by ticket -- get_closed_trades_range's "ticket" is the
position_id, matching the trade_exited/trade_closed_tp "ticket" field),
at several horizons, per account.

Dollar figures use each trade's own real volume (XAUUSD: $100 per lot
per $1 of price), so they are directly comparable to real P/L.

Trades whose exit is more recent than the horizon are EXCLUDED for that
horizon (their window would be truncated and would understate movement).

    python scripts/analyze_post_exit_movement.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, validate_account_name, load_config
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

HORIZONS_MINUTES = [15, 30, 60]
USD_PER_LOT_PER_DOLLAR = 100.0  # XAUUSD


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def read_categories(account: str) -> dict[int, str]:
    """ticket -> close category, from decisions.jsonl. trade_exited
    carries an explicit "category"; trade_closed_tp is the broker-side
    TP fill / external close path (see each engine's
    _record_broker_closed) and is recorded as take_profit."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out: dict[int, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ticket = e.get("ticket")
        if ticket is None:
            continue
        if e.get("action") == "trade_exited":
            out[int(ticket)] = e.get("category", "unknown")
        elif e.get("action") == "trade_closed_tp":
            out[int(ticket)] = "take_profit"
    return out


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        categories = read_categories(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=1), now)
        finally:
            connector.disconnect()

        trades = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            trades.append({**t, "_exit_utc": t["exit_time"].astimezone(timezone.utc),
                           "_category": categories.get(int(t["ticket"]), "unmatched")})
        if not trades:
            print(f"{account}: no trades in this window.\n")
            continue

        print(f"{'=' * 78}\n{account}: {len(trades)} real closed trades, since {args.since}\n{'=' * 78}")

        for horizon in HORIZONS_MINUTES:
            # Bucket by category: [favorable_usd_total, adverse_usd_total, count]
            agg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
            excluded = 0
            for t in trades:
                exit_utc = t["_exit_utc"]
                window_end = exit_utc + timedelta(minutes=horizon)
                if window_end > now:
                    excluded += 1
                    continue
                window = df[(df.index > exit_utc) & (df.index <= window_end)]
                if window.empty:
                    excluded += 1
                    continue
                exit_price = float(t["exit_price"])
                if t["direction"] == "BUY":
                    favorable = float(window["high"].max()) - exit_price
                    adverse = exit_price - float(window["low"].min())
                else:
                    favorable = exit_price - float(window["low"].min())
                    adverse = float(window["high"].max()) - exit_price
                usd = float(t["volume"]) * USD_PER_LOT_PER_DOLLAR
                bucket = agg[t["_category"]]
                bucket[0] += max(favorable, 0.0) * usd
                bucket[1] += max(adverse, 0.0) * usd
                bucket[2] += 1

            total_n = sum(b[2] for b in agg.values())
            print(f"  -- {horizon} minutes after exit -- ({total_n} trades, {excluded} excluded as too recent)")
            if total_n == 0:
                print("     no trades with a complete window at this horizon.\n")
                continue
            for category in sorted(agg, key=lambda c: -agg[c][2]):
                fav_usd, adv_usd, n = agg[category]
                print(f"     {category:<28} n={n:<4} avg further IN favor ${fav_usd / n:+7.2f}   "
                      f"avg further AGAINST ${adv_usd / n:+7.2f}")
            print()


if __name__ == "__main__":
    main()
