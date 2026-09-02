"""Research script (read-only, no live behavior change): does a
higher-timeframe trend filter -- trading only in the direction of a
slower moving average -- explain/avoid real losses?

Motivation (2026-09-02): every entry-quality filter tried so far in this
project (candle color, tick volume, EMA13/21 gap, ATR bucket, candle
decisiveness) is a SAME-CANDLE micro-feature. None of them look at the
bigger trend a trade might be fighting. A real loss cluster the same day
(demo1_m1/demo1_m3/demo2_m1/demo2_m3, 04:21-06:47 Colombo) showed price
falling steadily (ATR-14 climbing 0.82->5.65) while the bot kept taking
fresh BUY signals straight into the decline -- classic counter-trend
whipsaw, the textbook case a trend filter exists to catch.

Two variants tested, trade by trade, no lookahead (EMA is causal by
construction -- each value only depends on data up to and including that
row, same reasoning as compute_emas() itself):
  - EMA50 filter: BUY only kept if the confirming candle's close > EMA50;
    SELL only kept if close < EMA50.
  - EMA100 filter: same idea, slower/stricter.

Also does a WALK-FORWARD split (first half of the real trades by time vs
second half) per the standing process-improvement request (2026-09-02):
don't trust a filter that only looks good on the full pooled sample --
check it holds up in both halves independently, not just in aggregate.

    python scripts/analyze_trend_filter.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import validate_account_name, load_config
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

WARMUP_DAYS = 10  # generous warm-up so EMA100 settles well before `since`


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


def report(label: str, rows: list[dict], keep_fn) -> None:
    if not rows:
        print(f"    {label}: no trades in this slice.")
        return
    actual_total = sum(r["profit"] for r in rows)
    actual_wins = sum(1 for r in rows if r["profit"] > 0)
    kept = [r for r in rows if keep_fn(r)]
    skipped = [r for r in rows if not keep_fn(r)]
    kept_total = sum(r["profit"] for r in kept)
    kept_wins = sum(1 for r in kept if r["profit"] > 0)
    diff = kept_total - actual_total
    print(f"    {label} ({len(rows)} trades, actual P/L ${actual_total:+.2f}, {actual_wins} wins):")
    print(f"      Would keep: {len(kept)} trades, {kept_wins} wins "
          f"({100 * kept_wins / len(kept) if kept else 0:.1f}%), P/L ${kept_total:+.2f}")
    print(f"      Would skip: {len(skipped)} trades, P/L ${sum(r['profit'] for r in skipped):+.2f}")
    print(f"      -> {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f} vs actual "
          f"(skipped {100 * len(skipped) / len(rows):.0f}% of trades)")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            mt5_trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=WARMUP_DAYS), to)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["ema100"] = df["close"].ewm(span=100, adjust=False).mean()

        rows = []
        for t in mt5_trades:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            if candle_time is None:
                continue
            row = df.loc[candle_time]
            close = float(row["close"])
            with_trend_50 = (close > row["ema50"]) if t["direction"] == "BUY" else (close < row["ema50"])
            with_trend_100 = (close > row["ema100"]) if t["direction"] == "BUY" else (close < row["ema100"])
            rows.append({
                "time": entry_utc, "direction": t["direction"], "profit": t["profit"],
                "with_trend_50": bool(with_trend_50), "with_trend_100": bool(with_trend_100),
            })

        if not rows:
            print(f"{account}: no matched trades in this window.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        print(f"{'=' * 70}\n{account}: {len(rows)} real trades matched, since {args.since}\n{'=' * 70}")

        mid = len(rows) // 2
        first_half, second_half = rows[:mid], rows[mid:]

        for label, keep_fn in [
            ("EMA50 trend filter", lambda r: r["with_trend_50"]),
            ("EMA100 trend filter", lambda r: r["with_trend_100"]),
        ]:
            print(f"  {label}:")
            print(f"  -- Full sample --")
            report("Full", rows, keep_fn)
            print(f"  -- Walk-forward split --")
            report("First half (by time)", first_half, keep_fn)
            report("Second half (by time)", second_half, keep_fn)
            print()


if __name__ == "__main__":
    main()
