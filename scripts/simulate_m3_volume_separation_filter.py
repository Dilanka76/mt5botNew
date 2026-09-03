"""Trade-by-trade, no-lookahead replay testing whether tick-volume and/or
EMA13/21-separation would improve REAL M3 results, since both signals
point the same direction on M3 specifically (unlike color+volume, which
canceled out because they favor opposite timeframes -- see
project_demo3_entryfilter_research memory for that ruling).

Volume: already validated via scripts/simulate_demo3_entry_filter.py
(+$151.02 on demo1_m3, but -$59.82 on demo1_m1 -- M3-only finding).
EMA13/21 separation ("wide is best"): only ever bucket-correlated before
(scripts/analyze_entry_quality.py), never properly trade-by-trade
simulated -- this is that missing check. "Wide" here uses a ROLLING
top-third threshold over the trailing ~500 candles (same
candles_to_fetch window and same methodology as low_volume()'s rolling
bottom-third), NOT the whole-sample static tertile analyze_entry_quality.py
uses for its retrospective buckets -- that would be lookahead bias in a
trade-by-trade replay.

Tests three variants (Volume-only, Separation-only, Combined-both-required)
on demo1_m3/demo2_m3 ONLY (M1 excluded -- neither signal holds there per
prior validated research), WITH a walk-forward split (first half of real
trades by time vs second half) -- a filter only counts as real if both
halves agree, same discipline as scripts/analyze_trend_filter.py.

    python scripts/simulate_m3_volume_separation_filter.py --accounts demo1_m3,demo2_m3 --since "2026-08-25 00:00:00"

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

CANDLES_TO_FETCH = 500  # matches the live engine's rolling-window size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m3,demo2_m3")
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


def rolling_flags(df: pd.DataFrame, candle_time: pd.Timestamp) -> tuple[bool, bool]:
    """Same no-lookahead rolling-window approach as the live shadow
    logging: trailing CANDLES_TO_FETCH candles ending at (and including)
    candle_time. Returns (is_high_volume, is_wide_separation)."""
    pos = df.index.get_loc(candle_time)
    start = max(0, pos - (CANDLES_TO_FETCH - 1))
    vol_window = df["tick_volume"].iloc[start:pos + 1]
    vol_threshold = vol_window.quantile(2 / 3)  # top third = high volume
    high_volume = float(df.loc[candle_time, "tick_volume"]) > vol_threshold

    gap_window = (df["ema13"] - df["ema21"]).abs().iloc[start:pos + 1]
    gap_threshold = gap_window.quantile(2 / 3)  # top third = wide separation
    wide_gap = float(abs(df.loc[candle_time, "ema13"] - df.loc[candle_time, "ema21"])) > gap_threshold

    return high_volume, wide_gap


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
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=5), to)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        rows = []
        for t in mt5_trades:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            if candle_time is None:
                continue
            high_volume, wide_gap = rolling_flags(df, candle_time)
            rows.append({
                "time": entry_utc, "profit": t["profit"],
                "high_volume": high_volume, "wide_gap": wide_gap,
            })

        if not rows:
            print(f"{account}: no matched trades in this window.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        print(f"{'=' * 70}\n{account}: {len(rows)} real trades matched, since {args.since}\n{'=' * 70}")

        mid = len(rows) // 2
        first_half, second_half = rows[:mid], rows[mid:]

        for label, keep_fn in [
            ("Volume-only (top-third)", lambda r: r["high_volume"]),
            ("Separation-only (top-third)", lambda r: r["wide_gap"]),
            ("Combined (both required)", lambda r: r["high_volume"] and r["wide_gap"]),
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
