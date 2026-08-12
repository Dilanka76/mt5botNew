"""Backtest-only analysis: for the strategy's actual historical
immediate-entry trades (unchanged otherwise — reads them from an
existing scripts/backtest.py run's .trades.jsonl), tests whether the
gap size itself (distance between the cross candle's close and EMA13 at
entry — currently only used to decide immediate-vs-wait, never as a
filter on its own) predicts the outcome.

Scoped to immediate-entry trades only (gap < gap_threshold_usd by
definition — the EMA5-touch-wait path's gap is measured at the original
cross candle, not the later touch candle, and isn't reconstructable from
the entry candle alone the way this script works).

Two modes, same shape as scripts/volatility_filter_analysis.py:

1. No --min-gap given: prints a diagnostic table, win rate and P/L by
   gap quintile.

        python scripts/gap_size_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11

2. --min-gap given (optionally with --max-gap too): simulates "skip
   immediate entries whose gap falls outside this range" and reports
   the before/after aggregate change.

        python scripts/gap_size_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11 --min-gap 2.0

Gap is recomputed independently here (close - EMA13, mirrored by
direction — see bot/strategy/cross_detector.py::calculate_gap, not
stored in trades.jsonl today) using EMA13 computed the same way the live
strategy does (bot/indicators/ema.py), over the same warm-up-padded
range scripts/backtest.py itself uses, so the values line up with what
the engine actually saw at each entry.

Only re-fetches OHLC (to compute EMA13) — never touches live/demo
trading or re-runs the strategy engine. Skipping an entry needs no
engine re-simulation for the same reason explained in
volatility_filter_analysis.py: every cross this strategy acts on is
price/EMA-determined, not dependent on which trades were actually taken.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.trade_stats import compute_day_stats

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--min-gap", type=float, default=None, help="If given, simulate keeping only immediate entries with gap >= this (add --max-gap for a band)")
    parser.add_argument("--max-gap", type=float, default=None, help="Optional upper bound — combine with --min-gap to test a middle gap-size band")
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


def is_immediate_entry(trade: dict) -> bool:
    return trade.get("entry_type") == "immediate"


def main() -> None:
    args = parse_args()
    trades = load_trades(args.account, args.date_from, args.date_to)
    immediate = [t for t in trades if is_immediate_entry(t)]
    other = [t for t in trades if not is_immediate_entry(t)]
    print(f"Loaded {len(trades)} trades from the existing {args.account} backtest "
          f"({len(immediate)} immediate-entry — the only ones this script can score, "
          f"{len(other)} EMA5-touch-wait — left untouched either way).\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from_dt - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to_dt)
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    df = compute_emas(df, config.ema_periods)

    for t in immediate:
        open_time = pd.Timestamp(t["open_time"])
        idx = df.index.searchsorted(open_time)
        if 0 <= idx < len(df):
            close = df["close"].iloc[idx]
            ema13 = df["ema13"].iloc[idx]
            t["_gap"] = abs(float(close) - float(ema13))
        else:
            t["_gap"] = None

    scored = [t for t in immediate if t["_gap"] is not None]
    print(f"{len(scored)} of {len(immediate)} immediate-entry trades matched to a candle.\n")

    if args.min_gap is None:
        print("Gap-size quintile diagnostic — immediate-entry trades only, ranked smallest to largest gap at entry:\n")
        scored.sort(key=lambda t: t["_gap"])
        n = len(scored)
        quintile_size = max(1, n // 5)
        print(f"{'Quintile':10}{'Gap range':22}{'Trades':>8}{'Wins':>7}{'Win %':>8}{'Total P/L':>12}")
        for q in range(5):
            start = q * quintile_size
            end = (q + 1) * quintile_size if q < 4 else n
            bucket = scored[start:end]
            if not bucket:
                continue
            stats = compute_day_stats(bucket)
            gap_range = f"{bucket[0]['_gap']:.2f}-{bucket[-1]['_gap']:.2f}"
            label = ["Q1 (smallest)", "Q2", "Q3", "Q4", "Q5 (largest)"][q]
            print(f"{label:14}{gap_range:22}{stats['total_trades']:>8}{stats['wins']:>7}{stats['win_rate']:>7.1f}%{stats['total_pl']:>12.2f}")
        print("\nGap here only ranges 0 to gap_threshold_usd (immediate entries only, by definition) —")
        print("pick a --min-gap/--max-gap band from whichever quintile(s) look best and re-run with those values.")
        return

    upper = args.max_gap if args.max_gap is not None else float("inf")
    kept = [t for t in scored if args.min_gap <= t["_gap"] <= upper]
    skipped = [t for t in scored if not (args.min_gap <= t["_gap"] <= upper)]
    new_trades = kept + other  # EMA5-touch-wait entries are never filtered by this rule

    old_summary = compute_day_stats(trades)
    new_summary = compute_day_stats(new_trades)

    if args.max_gap is not None:
        print(f"Simulating: keep immediate entries only when {args.min_gap:.2f} <= gap <= {args.max_gap:.2f} at entry time.\n")
    else:
        print(f"Simulating: skip immediate entries with gap < {args.min_gap:.2f} at entry time.\n")
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
    print("\nMethodology note: EMA13 is recomputed independently here (same formula and warm-up padding")
    print("as the live strategy), not read back from the original backtest run — directional evidence.")


if __name__ == "__main__":
    main()
