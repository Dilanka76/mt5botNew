"""Research script (read-only, no live behavior change): how much does
real execution lag actually cost on entry, in dollars?

Motivation (2026-09-02): main.py's loop is polling-based, not
event-driven (see main.py's while-True loop) -- it only notices a new
candle closed once per config.tick_poll_interval_seconds (1s in the
template config), then still has to fetch OHLC, recompute EMAs, and send
the order to the broker before a fill actually happens. During that
window real price keeps moving. A real example (11:26 app time,
2026-09-02, demo1_m1 vs demo2_m1 on the identical signal) showed one
account fill $0.22 away from the theoretical candle-close price and the
other only $0.02 away -- this script checks whether that's meaningful,
systematic cost across many real trades, or just symmetric noise
averaging near zero (in which case tightening tick_poll_interval_seconds
wouldn't be worth the extra MT5 API load on this project's small EC2
instance -- see [[project_overview]]).

For every real trade_entered event in decisions.jsonl since --since,
finds the confirming candle (matching direction + EMA13/21 state, same
approach as every other simulate_*/analyze_*.py script this session) and
compares:
  - theoretical price: that candle's own real close
  - actual price: the real fill price already logged in decisions.jsonl
  - lag: log_decision's own timestamp (true UTC, logged right after the
    real order_send() call returns) minus the candle's own close time
    (candle open time + timeframe duration) -- an approximation of total
    detection+processing+network lag, not a precise instrument reading.

Slippage is reported SIGNED so cost and benefit don't cancel out
invisibly: positive = fill was WORSE than the theoretical close (paid
more on a BUY, received less on a SELL), negative = fill was BETTER.

    python scripts/measure_entry_slippage.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from bot.config import validate_account_name, load_config
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

TIMEFRAME_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def read_entries(account: str, since: datetime) -> list[dict]:
    config = load_config(account)
    log_dir = Path(config.logging.log_dir) / account
    path = log_dir / "decisions.jsonl"
    entries = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["timestamp"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if entry.get("action") != "trade_entered" or ts < since:
            continue
        entries.append({**entry, "timestamp": ts})
    return entries


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        tf_minutes = TIMEFRAME_MINUTES[config.timeframe]
        entries = read_entries(account, since)
        if not entries:
            print(f"{account}: no trade_entered events in this window.\n")
            continue

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=1), to)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        rows = []
        for e in entries:
            candle_time = find_confirming_candle(df, e["timestamp"], e["direction"])
            if candle_time is None:
                continue
            theoretical = float(df.loc[candle_time, "close"])
            actual = float(e["entry"])
            if e["direction"] == "BUY":
                slippage = actual - theoretical
            else:
                slippage = theoretical - actual
            candle_close_time = candle_time + timedelta(minutes=tf_minutes)
            lag_seconds = (e["timestamp"] - candle_close_time).total_seconds()
            # $ per $1 price move -- reuse the lots this specific trade
            # actually used (position sizing can change with balance over
            # the window), XAUUSD: $100/lot per $1 move.
            dollar_cost = slippage * float(e.get("lots", 0.0)) * 100
            rows.append({
                "time": e["timestamp"], "direction": e["direction"],
                "theoretical": theoretical, "actual": actual,
                "slippage": slippage, "lag_seconds": lag_seconds,
                "dollar_cost": dollar_cost,
            })

        if not rows:
            print(f"{account}: no matched trades in this window.\n")
            continue

        n = len(rows)
        total_cost = sum(r["dollar_cost"] for r in rows)
        avg_slippage = sum(r["slippage"] for r in rows) / n
        avg_lag = sum(r["lag_seconds"] for r in rows) / n
        worse = [r for r in rows if r["slippage"] > 0]
        better = [r for r in rows if r["slippage"] < 0]
        exact = n - len(worse) - len(better)
        max_worse = max((r["slippage"] for r in rows), default=0.0)
        max_better = min((r["slippage"] for r in rows), default=0.0)

        print(f"{'=' * 70}\n{account}: {n} real entries matched, since {args.since}\n{'=' * 70}")
        print(f"  Avg slippage: ${avg_slippage:+.3f}/price-unit (positive = fill worse than theoretical close)")
        print(f"  Avg detection+execution lag: {avg_lag:+.2f}s (candle-close to real fill log)")
        print(f"  Worse-than-close fills: {len(worse)} ({100*len(worse)/n:.0f}%), best/worst: "
              f"${max_worse:+.2f}")
        print(f"  Better-than-close fills: {len(better)} ({100*len(better)/n:.0f}%), best: ${max_better:+.2f}")
        print(f"  Exact match: {exact}")
        print(f"  TOTAL real $ cost from slippage this window: ${total_cost:+.2f} "
              f"(negative = net cost, positive = net benefit)\n")


if __name__ == "__main__":
    main()
