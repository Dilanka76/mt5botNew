"""Research script (read-only, no live behavior change): is the CURRENT
fixed-dollar stop-loss well-matched to real volatility, or does it sit
too tight/too loose depending on the moment?

Motivation (2026-09-02): every account uses a fixed stop_loss_usd ($5 on
M1, $10 on M3) regardless of how volatile the market is at entry time.
ATR-14 on real losing trades has been observed ranging from 0.82 to 5.65
in a single session (see [[project_trend_filter_research]]'s loss-cluster
writeup) -- a fixed $5 stop in a $5.65-ATR regime sits under 1x ATR,
close to guaranteed to get clipped by ordinary noise; the same $5 stop
in a $0.82-ATR regime sits over 6x ATR, unnecessarily loose. This is
STEP 1 of the standard two-step process this project uses (see
[[project_demo3_entryfilter_research]]): a retrospective bucket
correlation first (this script, same style as
scripts/analyze_entry_quality.py) to check whether a real relationship
exists at all, BEFORE building the much more involved trade-by-trade
counterfactual-price-path simulation an actual "ATR-scaled stop" rule
would need (varying the stop changes WHEN a trade would have closed, not
just whether it triggers -- a materially bigger undertaking than the
keep/skip entry filters already ported into shadow logging).

For every real trade since --since, computes stop_ratio =
config.stop_loss_usd / ATR-14 at the confirming candle (ATR computed the
same way scripts/show_losses_today.py already does -- simple rolling
mean of true range, shifted by 1 so it never includes the entry candle
itself). Buckets into <1.0x / 1.0-2.0x / >2.0x ATR and reports real
win rate / P&L per bucket, WITH a walk-forward split (first half of
trades by time vs second half) -- a bucket only counts as a real signal
if both halves agree, same discipline as
scripts/analyze_trend_filter.py.

    python scripts/analyze_atr_stop_ratio.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

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

WARMUP_DAYS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Same definition as scripts/show_losses_today.py's compute_atr()
    (kept identical deliberately -- this project already trusts this
    exact ATR number from that script's real loss breakdowns). Simple
    rolling mean of true range, shift(1) so a row's ATR never includes
    its own candle -- purely retrospective, no lookahead."""
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


def bucket_label(ratio: float) -> str:
    if ratio < 1.0:
        return "<1.0x ATR (tight)"
    if ratio < 2.0:
        return "1.0-2.0x ATR (moderate)"
    return ">2.0x ATR (loose)"


def report_slice(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"    {label}: no trades.")
        return
    print(f"    {label} ({len(rows)} trades):")
    for bucket in ["<1.0x ATR (tight)", "1.0-2.0x ATR (moderate)", ">2.0x ATR (loose)"]:
        bucket_rows = [r for r in rows if r["bucket"] == bucket]
        if not bucket_rows:
            print(f"      {bucket}: 0 trades")
            continue
        wins = sum(1 for r in bucket_rows if r["profit"] > 0)
        total = sum(r["profit"] for r in bucket_rows)
        avg_ratio = sum(r["ratio"] for r in bucket_rows) / len(bucket_rows)
        print(f"      {bucket}: {len(bucket_rows)} trades, {wins} wins "
              f"({100 * wins / len(bucket_rows):.1f}%), P/L ${total:+.2f}, avg ratio {avg_ratio:.2f}x")


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
        atr_series = compute_atr(df)

        rows = []
        for t in mt5_trades:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            if candle_time is None:
                continue
            atr = atr_series.loc[candle_time]
            if pd.isna(atr) or atr <= 0:
                continue
            ratio = config.stop_loss_usd / float(atr)
            rows.append({
                "time": entry_utc, "profit": t["profit"], "ratio": ratio,
                "bucket": bucket_label(ratio),
            })

        if not rows:
            print(f"{account}: no matched trades in this window.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        print(f"{'=' * 70}\n{account}: {len(rows)} real trades matched, since {args.since} "
              f"(stop_loss_usd=${config.stop_loss_usd:.2f})\n{'=' * 70}")
        report_slice("Full sample", rows)
        mid = len(rows) // 2
        print(f"  -- Walk-forward split --")
        report_slice("First half (by time)", rows[:mid])
        report_slice("Second half (by time)", rows[mid:])
        print()


if __name__ == "__main__":
    main()
