"""Replays demo1's REAL trade history with demo3's exact entry-filter
logic (bot/strategy/state_machine_dual_cross_confirmed_swap_adx_entryfilter.py's
_closed_in_favor()/_low_volume()/_entry_filter_reason(), reproduced here
identically) to answer: if this filter had actually been live, trade by
trade, what would the real dollar difference have been?

This is the stronger check the pooled win-rate-bucket analysis
(scripts/analyze_entry_quality.py) can't give on its own -- same
discipline as scripts/simulate_blocked_adx_signals.py, built after the
ADX-momentum filter looked good on a few real examples and then cost
$90 once properly traced against the full sample.

    python scripts/simulate_demo3_entry_filter.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

Volume threshold is computed the SAME way the live engine does: a
rolling quantile(1/3) over the trailing ~500 candles ending at (and
including) each trade's own confirming candle -- NOT a whole-sample
tertile like analyze_entry_quality.py uses for its retrospective
buckets, and NOT using any candle after the trade's own entry (would be
lookahead bias). This is a genuine trade-by-trade, in-order replay.

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

GAP_RE = re.compile(r"gap=(-?\d+\.?\d*)")
ENTRY_PAIR_WINDOW_SECONDS = 300
CANDLES_TO_FETCH = 500  # matches candles_to_fetch in the real per-account configs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
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


def closed_in_favor(direction: str, row: pd.Series) -> bool:
    if direction == "BUY":
        return row["close"] > row["open"]
    return row["close"] < row["open"]


def low_volume(df: pd.DataFrame, candle_time: pd.Timestamp) -> tuple[bool, float, float]:
    """Same definition as the live engine's _low_volume(): rolling
    quantile(1/3) over the trailing CANDLES_TO_FETCH candles ending at
    (and including) candle_time -- no future data. Returns
    (is_low_volume, actual_volume, threshold)."""
    pos = df.index.get_loc(candle_time)
    start = max(0, pos - (CANDLES_TO_FETCH - 1))
    window = df["tick_volume"].iloc[start:pos + 1]
    threshold = window.quantile(1 / 3)
    actual = float(df.loc[candle_time, "tick_volume"])
    return actual < threshold, actual, threshold


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
            # Extra warmup before `since` so the rolling volume window has
            # real history even for trades right at the start of the range.
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
            row = df.loc[candle_time]
            favor = closed_in_favor(t["direction"], row)
            low_vol, vol_actual, vol_threshold = low_volume(df, candle_time)

            rows.append({
                "time": entry_utc, "direction": t["direction"], "profit": t["profit"],
                "favor": favor, "low_vol": low_vol, "vol_actual": vol_actual,
                "vol_threshold": vol_threshold,
            })

        if not rows:
            print(f"{account}: no matched trades in this window.\n")
            continue

        rows.sort(key=lambda r: r["time"])
        actual_total = sum(r["profit"] for r in rows)
        actual_wins = sum(1 for r in rows if r["profit"] > 0)

        print(f"{'=' * 70}\n{account}: {len(rows)} real trades matched, since {args.since}\n{'=' * 70}")
        print(f"ACTUAL (no filter):  {len(rows)} trades, {actual_wins} wins "
              f"({100 * actual_wins / len(rows):.1f}%), total P/L ${actual_total:+.2f}\n")

        variants = [
            ("Color-only",  lambda r: r["favor"]),
            ("Volume-only", lambda r: not r["low_vol"]),
            ("Combined (both required)", lambda r: r["favor"] and not r["low_vol"]),
        ]

        def slice_report(indent: str, label: str, subset: list[dict], keep_fn) -> None:
            if not subset:
                print(f"{indent}{label}: no trades in this slice.")
                return
            sub_actual = sum(r["profit"] for r in subset)
            kept = [r for r in subset if keep_fn(r)]
            skipped = [r for r in subset if not keep_fn(r)]
            kept_total = sum(r["profit"] for r in kept)
            kept_wins = sum(1 for r in kept if r["profit"] > 0)
            diff = kept_total - sub_actual
            print(f"{indent}{label}: enter {len(kept)}/{len(subset)} "
                  f"({100 * kept_wins / len(kept) if kept else 0:.1f}% win), "
                  f"P/L ${kept_total:+.2f} vs actual ${sub_actual:+.2f} "
                  f"-> {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f} "
                  f"(skipped {100 * len(skipped) / len(subset):.0f}%)")

        # Walk-forward split, added 2026-09-03: a filter only counts as
        # real if BOTH halves agree independently -- full-sample-only
        # results have twice been misleading in this project (demo1's
        # color filter looked strong pooled then reversed per-timeframe;
        # the EMA-separation filter looked strong in buckets but was
        # unusable trade-by-trade). Same discipline as
        # scripts/analyze_trend_filter.py.
        mid = len(rows) // 2
        for label, keep_fn in variants:
            print(f"  {label}:")
            slice_report("    ", "Full sample ", rows, keep_fn)
            slice_report("    ", "First half  ", rows[:mid], keep_fn)
            slice_report("    ", "Second half ", rows[mid:], keep_fn)
            print()


if __name__ == "__main__":
    main()
