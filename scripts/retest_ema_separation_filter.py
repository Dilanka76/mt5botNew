"""Re-test of the EMA13/21-separation entry filter, with a CORRECTED
threshold.

Why this exists (2026-09-04): separation was previously "ruled out" by
scripts/simulate_m3_volume_separation_filter.py, which kept only 3-6
trades out of ~65 (91-95% skipped) and was therefore judged unusable.
That verdict was WRONG, and the flaw was in the test, not the idea:

  - scripts/analyze_entry_quality.py ranked separation across TRADES
    (whole-sample tertiles), so a third of entries land in "wide" by
    construction. That is where the promising numbers came from
    (demo1_m3 wide bucket 85.7% win, demo2_m3 80.0%).
  - The trade-by-trade test set the threshold at the top third of ALL
    RECENT CANDLES. But an EMA13/21 cross happens exactly where the two
    lines meet, so separation at any entry is tiny by definition
    (real entries show 0.01-0.20), while mid-trend candles sit dollars
    apart. Entry candles were being asked to clear a bar set by
    non-entry candles -- almost nothing could pass. The 95%-skip result
    was an artifact of the threshold, not evidence.

This version sets the threshold from the distribution of separation at
CROSS CANDLES ONLY -- i.e. "wide compared with other entry
opportunities", which is what the bucket analysis actually measured.
Cross candles are detected the same way every other script in this
project does it (a genuine EMA13/21 state change at candle close), and
the threshold is ROLLING over only the crosses that occurred BEFORE each
trade, so there is no lookahead.

Tests separation at several percentile cutoffs, with the walk-forward
split, per account.

    python scripts/retest_ema_separation_filter.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

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
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

PERCENTILES = [0.33, 0.50, 0.67]  # keep entries whose separation is above this percentile of prior crosses
MIN_PRIOR_CROSSES = 20  # need this many prior crosses before the rolling threshold is meaningful


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def cross_candle_separations(df: pd.DataFrame) -> pd.Series:
    """|ema13 - ema21| on every candle where the EMA13/21 state genuinely
    CHANGED versus the previous candle -- the same "confirmed cross"
    definition the live engines use. This is the correct reference
    population for judging whether a given entry's separation is wide."""
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False
    sep = (df["ema13"] - df["ema21"]).abs()
    return sep[changed]


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        row = w.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def report(label: str, rows: list[dict], keep_fn) -> None:
    usable = [r for r in rows if r["threshold"] is not None]
    if not usable:
        print(f"    {label}: no trades with a usable rolling threshold.")
        return
    actual = sum(r["profit"] for r in usable)
    kept = [r for r in usable if keep_fn(r)]
    skipped = [r for r in usable if not keep_fn(r)]
    ktot = sum(r["profit"] for r in kept)
    kwins = sum(1 for r in kept if r["profit"] > 0)
    diff = ktot - actual
    print(f"    {label}: keep {len(kept)}/{len(usable)} "
          f"({100*kwins/len(kept) if kept else 0:.1f}% win), P/L ${ktot:+.2f} vs actual ${actual:+.2f} "
          f"-> {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f} (skipped {100*len(skipped)/len(usable):.0f}%)")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=10), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        cross_seps = cross_candle_separations(df)

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_confirming_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            sep = abs(float(df.loc[ct, "ema13"]) - float(df.loc[ct, "ema21"]))
            # Rolling reference: only crosses that happened BEFORE this one.
            prior = cross_seps[cross_seps.index < ct]
            thresholds = None
            if len(prior) >= MIN_PRIOR_CROSSES:
                thresholds = {p: float(prior.quantile(p)) for p in PERCENTILES}
            rows.append({"time": entry_utc, "profit": t["profit"], "sep": sep, "threshold": thresholds})

        if not rows:
            print(f"{account}: no matched trades.\n")
            continue
        rows.sort(key=lambda r: r["time"])
        usable = [r for r in rows if r["threshold"] is not None]

        seps = sorted(r["sep"] for r in rows)
        print(f"{'=' * 78}\n{account}: {len(rows)} trades ({len(usable)} with a usable rolling threshold)")
        print(f"  entry separations: min {seps[0]:.3f}  median {seps[len(seps)//2]:.3f}  max {seps[-1]:.3f}")
        print(f"  reference population: {len(cross_seps)} cross candles, "
              f"median separation {float(cross_seps.median()):.3f}\n{'=' * 78}")

        mid = len(usable) // 2
        for p in PERCENTILES:
            keep_fn = lambda r, p=p: r["threshold"] is not None and r["sep"] > r["threshold"][p]
            print(f"  keep entries wider than the {int(p*100)}th percentile of prior crosses:")
            report("Full sample ", usable, keep_fn)
            report("First half  ", usable[:mid], keep_fn)
            report("Second half ", usable[mid:], keep_fn)
            print()


if __name__ == "__main__":
    main()
