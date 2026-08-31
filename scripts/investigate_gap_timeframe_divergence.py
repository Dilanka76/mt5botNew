"""Investigates WHY demo1_m1 and demo1_m3 show opposite results for the
gap-size/entry-type signal (full_strategy_analysis.py, 2026-08-31):
demo1_m3's wide-gap/ema5_touch entries clearly outperform its immediate
ones (86.7% vs 69.6% win), but demo1_m1 shows the OPPOSITE (immediate
+$12.08 avg, ema5_touch -$9.28 avg).

Working hypothesis, not yet confirmed: the SAME $5 (or $7 on m3 post-
2026-08-28) dollar gap threshold means something very different on a
1-minute candle than a 3-minute one, since typical candle range scales
with elapsed time. A $5 gap within one M1 candle is a much more extreme,
possibly-exhausted move than a $5-7 gap accumulated over one M3 candle.
If so, M1's "wide gap" bucket may be capturing overextended spikes (bad
to chase via EMA5 pullback), while M3's is capturing normal strong-trend
continuation (good to chase).

Tests this directly: for every real trade, computes ATR-14 at its
confirming candle (same simple rolling-mean True Range as
analyze_entry_quality.py/volatility_filter_analysis.py) and the ratio
gap/ATR -- a timeframe-normalized measure of "how extreme was this gap
relative to what's normal for this timeframe right now." Compares that
ratio's distribution between immediate and ema5_touch buckets, on each
account separately.

    python scripts/investigate_gap_timeframe_divergence.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical candles, never
touches live/demo trading.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so sibling scripts are importable

import argparse

import pandas as pd

from bot.data.market_data import get_ohlc_range  # noqa: E402
from bot.indicators.ema import compute_emas  # noqa: E402
from bot.mt5_connector import MT5Connector  # noqa: E402
from full_strategy_analysis import GAP_CHANGE_CUTOVER_UTC, _gap_threshold_at  # noqa: E402
from generate_live_test_report import gather_account_data, load_config  # noqa: E402


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period).mean().shift(1)


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def summarize_bucket(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"    {label:<14} n=  0")
        return
    n = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "WIN")
    pl = sum(r["profit"] for r in rows)
    gaps = [r["entry_gap"] for r in rows]
    ratios = [r["_gap_atr_ratio"] for r in rows if r["_gap_atr_ratio"] is not None]
    base = f"    {label:<14} n={n:>3}  win={100 * wins / n:5.1f}%  P/L=${pl:+8.2f}  avg=${pl / n:+.2f}  avg_gap=${mean(gaps):.2f}"
    if ratios:
        print(f"{base}  avg_gap/ATR={mean(ratios):.2f}")
    else:
        print(f"{base}  (ATR unavailable for any trade in this bucket)")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    accounts = [a.strip() for a in args.accounts.split(",")]

    for account in accounts:
        timeframe = "M1" if account.endswith("_m1") else "M3"
        print(f"\n{'=' * 78}\nACCOUNT: {account} ({timeframe})\n{'=' * 78}")
        config = load_config(account)
        decisions, records, rules = gather_account_data(account, timeframe)
        records = [r for r in records if r["entry_gap"] is not None]  # exclude swap re-entries, no gap logged

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(hours=2), datetime.now(timezone.utc))
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        atr_series = compute_atr(df)

        for r in records:
            entry_utc = r["entry_time"].astimezone(timezone.utc)
            candle_time = find_confirming_candle(df, entry_utc, r["direction"])
            if candle_time is None or pd.isna(atr_series.loc[candle_time]):
                r["_atr"] = None
                r["_gap_atr_ratio"] = None
                continue
            atr = float(atr_series.loc[candle_time])
            r["_atr"] = atr
            r["_gap_atr_ratio"] = r["entry_gap"] / atr if atr > 0 else None
            threshold = _gap_threshold_at(account, entry_utc)
            r["_entry_type"] = "immediate" if r["entry_gap"] < threshold else "ema5_touch"

        immediate = [r for r in records if r.get("_entry_type") == "immediate"]
        ema5_touch = [r for r in records if r.get("_entry_type") == "ema5_touch"]

        print(f"\n  Timeframe-normalized comparison (gap size relative to this timeframe's own typical ATR-14):")
        summarize_bucket("immediate", immediate)
        summarize_bucket("ema5_touch", ema5_touch)

        ratios_all = [r["_gap_atr_ratio"] for r in records if r.get("_gap_atr_ratio") is not None]
        if ratios_all:
            print(f"\n  Overall gap/ATR ratio on this account: mean={mean(ratios_all):.2f}, median={median(ratios_all):.2f}, "
                  f"min={min(ratios_all):.2f}, max={max(ratios_all):.2f}")

    print(f"\n{'=' * 78}\nInterpretation guide: if demo1_m1's ema5_touch trades show a much\n"
          f"HIGHER avg gap/ATR ratio than demo1_m3's, that supports the hypothesis\n"
          f"that M1's wide-gap entries are relatively more extreme/overextended\n"
          f"moves than M3's -- a real, timeframe-specific reason for the divergence,\n"
          f"not just noise.\n{'=' * 78}")


if __name__ == "__main__":
    main()
