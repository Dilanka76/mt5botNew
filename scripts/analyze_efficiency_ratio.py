"""Full Efficiency Ratio analysis, per account, before deciding whether
to use it as a live rule.

Kaufman's Efficiency Ratio (the core of his Adaptive Moving Average) --
a STANDARD indicator, not invented here:

    ER = |close[t] - close[t-n]| / (total distance travelled over n bars)

Near 1.0 = price went somewhere in a straight line (trending).
Near 0 = lots of movement, no progress (chop). Scale-free 0..1, so a
fixed threshold means the same thing in all conditions -- unlike raw
tick volume, which needed a rolling percentile.

TWO DEFINITIONS TESTED, because the standard one has a real blind spot:
  close-only : denominator is the sum of |close-to-close| steps. Cannot
               see what happened INSIDE a candle -- a bar that spiked
               $6 up, crashed $12 down and closed flat reads as calm.
  true-range : denominator is the sum of true ranges (includes wicks),
               so intra-candle violence counts. Your stops are hit
               intra-candle, so this may predict outcomes better.

Reports, per account: the ER distribution, a bucket table with explicit
WIN and LOSS counts, and a filter simulation at fixed thresholds WITH
the walk-forward split -- because a full-sample-only result has reversed
three times on this project already.

    python scripts/analyze_efficiency_ratio.py --since "2026-08-25 00:00:00" --offset-hours 3

Read-only: connects to MT5 only to read historical data.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

BANDS = [(0.00, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.01)]
THRESHOLDS = [0.05, 0.10, 0.15, 0.20]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo2_m1,demo1_m3,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--lookback", type=int, default=20, help="candles used for the ratio (default 20)")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker offset, supplied deliberately when the market is closed (this broker: 3)")
    return p.parse_args()


def efficiency_ratios(df: pd.DataFrame, lookback: int) -> tuple[pd.Series, pd.Series]:
    """Returns (close_only, true_range) ER series, both causal -- the
    value at row i uses only rows i-lookback..i."""
    close = df["close"]
    net = (close - close.shift(lookback)).abs()

    step = close.diff().abs()
    total_close = step.rolling(lookback).sum()

    prev_close = close.shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    total_tr = true_range.rolling(lookback).sum()

    return (net / total_close).replace([float("inf")], float("nan")), \
           (net / total_tr).replace([float("inf")], float("nan"))


def find_cross_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    """The candle where the EMA13/21 state genuinely CHANGED -- not merely
    one where the EMAs sit on the right side (that bug produced garbage
    in measure_cross_to_entry_gap.py on 2026-09-04)."""
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    want_above = direction == "BUY"
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        if changed.loc[idx] and bool(above.loc[idx]) == want_above:
            return idx
    return None


def bucket_table(rows: list[dict], key: str) -> None:
    print(f"    {'ER band':<16}{'trades':<9}{'wins':<7}{'losses':<9}{'win rate':<11}{'total':<13}per trade")
    for lo, hi in BANDS:
        sel = [r for r in rows if lo <= r[key] < hi]
        if not sel:
            print(f"    {lo:.2f}-{hi if hi <= 1 else 1.0:.2f}       0")
            continue
        wins = [r for r in sel if r["profit"] > 0]
        losses = [r for r in sel if r["profit"] <= 0]
        total = sum(r["profit"] for r in sel)
        print(f"    {lo:.2f}-{min(hi,1.0):.2f}       {len(sel):<9}{len(wins):<7}{len(losses):<9}"
              f"{100*len(wins)/len(sel):>5.1f}%     ${total:>+9.2f}   ${total/len(sel):>+7.2f}")


def simulate(rows: list[dict], key: str) -> None:
    actual = sum(r["profit"] for r in rows)
    mid = len(rows) // 2
    print(f"    {'keep if ER >=':<16}{'kept':<8}{'win rate':<11}{'P/L':<13}{'vs actual':<14}walk-forward")
    for th in THRESHOLDS:
        kept = [r for r in rows if r[key] >= th]
        if not kept:
            print(f"    {th:.2f}            0        -- no trades kept --")
            continue
        ktot = sum(r["profit"] for r in kept)
        kwins = sum(1 for r in kept if r["profit"] > 0)
        diff = ktot - actual
        f_rows, s_rows = rows[:mid], rows[mid:]
        f_diff = sum(r["profit"] for r in f_rows if r[key] >= th) - sum(r["profit"] for r in f_rows)
        s_diff = sum(r["profit"] for r in s_rows if r[key] >= th) - sum(r["profit"] for r in s_rows)
        agree = "BOTH +" if f_diff > 0 and s_diff > 0 else ("BOTH -" if f_diff < 0 and s_diff < 0 else "SPLIT")
        print(f"    {th:.2f}            {len(kept):<8}{100*kwins/len(kept):>5.1f}%     "
              f"${ktot:>+9.2f}   {'ADDED' if diff>0 else 'COST':<5}${abs(diff):>8.2f}  "
              f"{f_diff:+8.2f}/{s_diff:+8.2f} {agree}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    print(f"Kaufman Efficiency Ratio, lookback {args.lookback} candles")
    if args.offset_hours is not None:
        print(f"NOTE: broker offset supplied manually as +{args.offset_hours}h (market closed)")
    print()

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=5), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        er_close, er_tr = efficiency_ratios(df, args.lookback)

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_cross_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            a, b = er_close.get(ct), er_tr.get(ct)
            if a is None or b is None or pd.isna(a) or pd.isna(b):
                continue
            rows.append({"time": entry_utc, "profit": t["profit"], "close_er": float(a), "tr_er": float(b)})
        if not rows:
            print(f"{account}: no matched trades.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        total = sum(r["profit"] for r in rows)
        wins = sum(1 for r in rows if r["profit"] > 0)
        print(f"{'=' * 88}\n{account} ({config.timeframe}): {len(rows)} trades, {wins} wins / "
              f"{len(rows)-wins} losses ({100*wins/len(rows):.1f}%), total ${total:+.2f}\n{'=' * 88}")

        for label, key in (("CLOSE-ONLY ER", "close_er"), ("TRUE-RANGE ER (includes wicks)", "tr_er")):
            vals = sorted(r[key] for r in rows)
            print(f"  {label}   median {statistics.median(vals):.3f}  "
                  f"range {vals[0]:.3f}-{vals[-1]:.3f}")
            bucket_table(rows, key)
            print()
            simulate(rows, key)
            print()


if __name__ == "__main__":
    main()
