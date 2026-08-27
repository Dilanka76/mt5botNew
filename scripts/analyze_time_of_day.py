"""Checks whether time-of-day genuinely predicts win rate in real trade
history -- built 2026-08-27 at the user's request ("if we can consider the
time our running time, this is very help us increase the accuracy").

analyze_entry_quality.py already tried an hour-of-day breakdown and found
it too noisy to trust (too few real trades landing in any single hour).
This script fixes that by using wider 4-hour buckets in MT5 APP time (the
same UTC+3 broker/server time your mobile app shows, matching the
convention already settled on for generate_live_test_report.py) across
EVERY real closed trade -- not just the small-gap subset -- so each bucket
actually has enough samples to mean something.

    python scripts/analyze_time_of_day.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

Read-only: only pulls real closed-trade history via
bot.analytics.get_closed_trades_range (already offset-corrected), never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

APP_TZ = timezone(timedelta(hours=3))
BUCKET_HOURS = 4
BUCKET_LABELS = [f"{h:02d}:00-{(h + BUCKET_HOURS) % 24:02d}:00" for h in range(0, 24, BUCKET_HOURS)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def bucket_label(entry_utc: datetime) -> str:
    app_hour = entry_utc.astimezone(APP_TZ).hour
    bucket_start = (app_hour // BUCKET_HOURS) * BUCKET_HOURS
    return BUCKET_LABELS[bucket_start // BUCKET_HOURS]


def summarize(rows: list[dict], title: str) -> None:
    print(f"\n{title}")
    groups: dict[str, list[dict]] = {label: [] for label in BUCKET_LABELS}
    for r in rows:
        groups[r["bucket"]].append(r)
    for label in BUCKET_LABELS:
        g = groups[label]
        if not g:
            print(f"    {label} (app time)   n=  0")
            continue
        wins = [r for r in g if r["profit"] > 0]
        win_rate = 100 * len(wins) / len(g)
        avg_pl = mean(r["profit"] for r in g)
        total = sum(r["profit"] for r in g)
        print(f"    {label} (app time)   n={len(g):>3}  win_rate={win_rate:5.1f}%  avg_P/L=${avg_pl:+7.2f}  total=${total:+8.2f}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    all_rows: list[dict] = []

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
        finally:
            connector.disconnect()

        rows = []
        for t in trades:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            rows.append({"profit": t["profit"], "bucket": bucket_label(entry_utc)})

        wins = [r for r in rows if r["profit"] > 0]
        print(f"\n{'=' * 78}\nACCOUNT: {account}  ({len(rows)} real trades, "
              f"{100 * len(wins) / len(rows) if rows else 0:.1f}% win rate, "
              f"total ${sum(r['profit'] for r in rows):+.2f})\n{'=' * 78}")
        summarize(rows, "By 4-hour app-time window:")
        all_rows.extend(rows)

    if len(accounts) > 1 and all_rows:
        wins = [r for r in all_rows if r["profit"] > 0]
        print(f"\n{'=' * 78}\nCOMBINED: {len(all_rows)} real trades, "
              f"{100 * len(wins) / len(all_rows):.1f}% win rate, "
              f"total ${sum(r['profit'] for r in all_rows):+.2f}\n{'=' * 78}")
        summarize(all_rows, "By 4-hour app-time window:")


if __name__ == "__main__":
    main()
