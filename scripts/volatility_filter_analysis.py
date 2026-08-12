"""Backtest-only analysis: for the strategy's actual historical
immediate-entry trades (unchanged otherwise — reads them from an
existing scripts/backtest.py run's .trades.jsonl), tests the idea that
low recent volatility (as measured by ATR, Average True Range) predicts
worse outcomes for the "gap < threshold, enter immediately" path — the
one that fires with no wait for confirmation.

Two modes:

1. No --min-atr given: prints a diagnostic table, win rate and P/L by
   ATR quintile, so a sensible threshold can be picked from real data
   rather than guessed.

        python scripts/volatility_filter_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11

2. --min-atr given: simulates "immediate entries are skipped whenever
   ATR at entry time was below this value" and reports the before/after
   aggregate change. EMA5-touch-wait entries are left untouched either
   way — they already have their own built-in confirmation wait; this
   idea (per the user's own framing) only targets the immediate path.

        python scripts/volatility_filter_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11 --min-atr 3.0

Skipping an entry does not require re-simulating the engine: every
cross this strategy acts on is determined purely by price/EMA data, not
by whether a position happens to be open at the time (the engine always
closes-and-immediately-reevaluates on every valid cross regardless), so
removing one entry from the trade list has no effect on the timing or
direction of any other trade. Only re-fetches OHLC (to compute ATR) —
never touches live/demo trading or re-runs the strategy engine.

ATR here is a simple rolling mean of True Range over --period candles
(default 14), computed using only candles strictly before the entry
candle (no lookahead) — a standard but simplified version of ATR
(Wilder's smoothing is the "textbook" version; this is close enough for
a directional read, not exact).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.trade_stats import compute_day_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--period", type=int, default=14, help="ATR lookback period in candles (default: 14)")
    parser.add_argument("--min-atr", type=float, default=None, help="If given, simulate skipping immediate entries below this ATR instead of showing the quintile diagnostic")
    return parser.parse_args()


def load_trades(account: str, date_from: str, date_to: str) -> list[dict]:
    path = PROJECT_ROOT / "reports" / "backtest" / account / f"{date_from}_{date_to}.trades.jsonl"
    if not path.exists():
        raise SystemExit(
            f"{path} not found — run scripts/backtest.py for this account/range first:\n"
            f"  python scripts/backtest.py --account {account} --from {date_from} --to {date_to}"
        )
    trades = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            trades.append(json.loads(line))
    return trades


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.rolling(period).mean()
    # shift(1): a trade entering on candle i can only have known ATR as of
    # candle i-1's close — using candle i's own (not-yet-closed-at-decision-
    # time) range would be lookahead.
    return atr.shift(1)


def is_immediate_entry(trade: dict) -> bool:
    return "immediate entry" in trade.get("reason", "")


def main() -> None:
    args = parse_args()
    trades = load_trades(args.account, args.date_from, args.date_to)
    immediate = [t for t in trades if is_immediate_entry(t)]
    waited = [t for t in trades if not is_immediate_entry(t)]
    print(f"Loaded {len(trades)} trades from the existing {args.account} backtest "
          f"({len(immediate)} immediate-entry, {len(waited)} EMA5-touch-wait).\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, date_from_dt, date_to_dt)
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    atr = compute_atr(df, args.period)

    for t in immediate:
        open_time = pd.Timestamp(t["open_time"])
        idx = df.index.searchsorted(open_time)
        t["_atr"] = float(atr.iloc[idx]) if 0 <= idx < len(atr) and pd.notna(atr.iloc[idx]) else None

    scored = [t for t in immediate if t["_atr"] is not None]
    print(f"{len(scored)} of {len(immediate)} immediate-entry trades have a valid ATR reading "
          f"(the rest fall in the first {args.period} candles of the range, before ATR warms up).\n")

    if args.min_atr is None:
        print(f"ATR({args.period}) quintile diagnostic — immediate-entry trades only, ranked lowest to highest volatility at entry:\n")
        scored.sort(key=lambda t: t["_atr"])
        n = len(scored)
        quintile_size = max(1, n // 5)
        print(f"{'Quintile':10}{'ATR range':22}{'Trades':>8}{'Wins':>7}{'Win %':>8}{'Total P/L':>12}")
        for q in range(5):
            start = q * quintile_size
            end = (q + 1) * quintile_size if q < 4 else n
            bucket = scored[start:end]
            if not bucket:
                continue
            stats = compute_day_stats(bucket)
            atr_range = f"{bucket[0]['_atr']:.2f}-{bucket[-1]['_atr']:.2f}"
            label = ["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"][q]
            print(f"{label:10}{atr_range:22}{stats['total_trades']:>8}{stats['wins']:>7}{stats['win_rate']:>7.1f}%{stats['total_pl']:>12.2f}")
        print("\nIf win rate and P/L climb from Q1 to Q5, low volatility is genuinely predictive here —")
        print("pick a --min-atr near the boundary where it starts turning positive and re-run with that value.")
        return

    kept = [t for t in scored if t["_atr"] >= args.min_atr]
    skipped = [t for t in scored if t["_atr"] < args.min_atr]
    new_trades = kept + waited  # EMA5-touch-wait entries are never filtered by this rule

    old_summary = compute_day_stats(trades)
    new_summary = compute_day_stats(new_trades)

    print(f"Simulating: skip immediate entries with ATR({args.period}) < {args.min_atr:.2f} at entry time.\n")
    print(f"{'':22}{'BEFORE':>14}{'AFTER':>14}")
    print(f"{'Total trades':22}{old_summary['total_trades']:>14}{new_summary['total_trades']:>14}")
    print(f"{'Wins':22}{old_summary['wins']:>14}{new_summary['wins']:>14}")
    print(f"{'Losses':22}{old_summary['losses']:>14}{new_summary['losses']:>14}")
    print(f"{'Win rate':22}{old_summary['win_rate']:>13.1f}%{new_summary['win_rate']:>13.1f}%")
    print(f"{'Total P/L':22}{old_summary['total_pl']:>14.2f}{new_summary['total_pl']:>14.2f}")
    print(f"{'Avg win':22}{old_summary['avg_win']:>14.2f}{new_summary['avg_win']:>14.2f}")
    print(f"{'Avg loss':22}{old_summary['avg_loss']:>14.2f}{new_summary['avg_loss']:>14.2f}")

    skipped_stats = compute_day_stats(skipped)
    print(f"\n{len(skipped)} immediate entries would have been skipped — as a group they were "
          f"{skipped_stats['win_rate']:.1f}% win rate, {skipped_stats['total_pl']:+.2f} total P/L "
          f"(compare to the overall {old_summary['win_rate']:.1f}% / {old_summary['total_pl']:+.2f} above).")
    print("\nMethodology note: candle high/low stand in for two synthetic ticks in the underlying")
    print("backtest (same approximation as scripts/backtest.py); ATR itself is a simple rolling mean")
    print("of True Range (not Wilder's smoothing) — directional evidence, not an exact simulation.")


if __name__ == "__main__":
    main()
