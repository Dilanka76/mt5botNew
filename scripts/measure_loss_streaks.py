"""How often do losing streaks of each length actually happen?

Needed to pick N for a "pause after N consecutive losses" risk rule
(2026-09-04). The trap: at a ~45% win rate, losing streaks are NORMAL.
Pure chance at demo2_m1's 44% win rate produces a 3-loss streak roughly
every other day -- a limit set at 3 would leave the bot paused most of
the time for nothing unusual. The rule is only meaningful if it fires on
runs that are genuinely rare.

Theory alone is not enough either: this project has shown that real
trades CLUSTER (losses bunch together in choppy conditions -- see
project_trend_filter_research's clustering section), so real streaks
should be longer and more frequent than an independent-coin-flip model
predicts. This measures the real thing.

For each account, reports:
  - how many times each streak length actually occurred
  - how often that is, per trading day
  - what an independent coin flip at that account's own win rate would
    have predicted, so the gap shows how much clustering there really is
  - the worst streak, and what it cost

Trading day boundary is 03:30 Sri Lanka time, matching every other
report in this project.

    python scripts/measure_loss_streaks.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read real closed-trade history.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
DAY_BOUNDARY = timedelta(hours=3, minutes=30)
STREAK_LENGTHS = [2, 3, 4, 5, 6, 7, 8]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def trading_day(dt: datetime):
    return (dt.astimezone(COLOMBO) - DAY_BOUNDARY).date()


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
        finally:
            connector.disconnect()

        trades = sorted(
            (t for t in raw if t["entry_time"].astimezone(timezone.utc) >= since),
            key=lambda t: t["entry_time"],
        )
        if not trades:
            print(f"{account}: no trades.\n")
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t["profit"] > 0)
        win_rate = wins / n
        loss_rate = 1 - win_rate
        days = len({trading_day(t["entry_time"]) for t in trades})

        # Walk the sequence and record every completed losing run.
        runs: list[tuple[int, float]] = []   # (length, total $ lost in that run)
        cur_len, cur_pl = 0, 0.0
        for t in trades:
            if t["profit"] <= 0:
                cur_len += 1
                cur_pl += t["profit"]
            else:
                if cur_len:
                    runs.append((cur_len, cur_pl))
                cur_len, cur_pl = 0, 0.0
        if cur_len:
            runs.append((cur_len, cur_pl))

        by_len = Counter(length for length, _ in runs)

        print(f"{'=' * 78}\n{account}: {n} trades over {days} trading days, "
              f"{100*win_rate:.1f}% win rate\n{'=' * 78}")
        print(f"  {'streak':<10}{'occurred':<12}{'per day':<12}{'random model':<16}worst cost")
        for k in STREAK_LENGTHS:
            # A run of length L contains (L - k + 1) streaks of length k.
            actual = sum(max(0, length - k + 1) for length, _ in runs if length >= k)
            if actual == 0:
                continue
            expected = n * (loss_rate ** k)
            worst = min((pl for length, pl in runs if length >= k), default=0.0)
            print(f"  {k} in a row{'':<1}{actual:<12}{actual/days:<12.2f}"
                  f"{expected:<16.1f}${worst:+.2f}")

        longest, longest_pl = max(runs, key=lambda r: r[0])
        print(f"\n  longest real streak: {longest} losses in a row, costing ${longest_pl:+.2f}")
        print(f"  ({len(runs)} losing runs in total)")
        print()


if __name__ == "__main__":
    main()
