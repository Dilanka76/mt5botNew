"""Reports every DISTINCT magic number that appears in an account's real
deal history over a given lookback window, with counts and the earliest/
latest timestamps for each — a foreign magic number showing up here (one
that doesn't belong to this account's own bots) is concrete evidence of a
real cross-account mixup, not just a theoretical risk.

Built 2026-08-27 after finding demo1_m1's bot (magic 910001) had placed a
real trade on demo2's account (922696) instead of demo1's own (740602) --
this checks whether the SAME kind of mixup has ALSO happened between
demo1 and live1 (which have been running together for much longer, before
demo2 was ever introduced).

    python scripts/check_magic_history.py --account live1 --days 30
    python scripts/check_magic_history.py --account demo1_m1 --days 30

Read-only -- only calls mt5.history_deals_get(), never touches any order.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.analytics import mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--days", type=int, default=30, help="How many days of history to scan (default 30).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        account_info = connector.account_info()
        print(f"account={args.account} connected login={account_info.login} server={account_info.server}\n")

        offset = mt5_utc_offset(connector, config.symbol)
        now_true_utc = datetime.now(timezone.utc)
        date_from_true = now_true_utc - timedelta(days=args.days)

        deals = mt5.history_deals_get(date_from_true + offset, now_true_utc + offset)
    finally:
        connector.disconnect()

    if not deals:
        print(f"No deals found in the last {args.days} day(s).")
        return

    by_magic: dict[int, list] = defaultdict(list)
    for d in deals:
        by_magic[d.magic].append(d)

    print(f"{'magic':>10}  {'count':>6}  {'first (true UTC)':<20}  {'last (true UTC)':<20}  sample comment")
    for magic in sorted(by_magic):
        group = by_magic[magic]
        times = sorted((datetime.fromtimestamp(d.time, tz=timezone.utc) - offset) for d in group)
        sample_comment = next((d.comment for d in group if d.comment), "")
        print(
            f"{magic:>10}  {len(group):>6}  {times[0].strftime('%Y-%m-%d %H:%M:%S'):<20}  "
            f"{times[-1].strftime('%Y-%m-%d %H:%M:%S'):<20}  {sample_comment}"
        )

    print(f"\n{len(deals)} total deal(s), {len(by_magic)} distinct magic number(s) over the last {args.days} day(s).")
    print("Expected magic numbers for this project: 900002 (retired demo1), 910001 (demo1_m1), "
          "910003 (demo1_m3), 910005 (retired demo1_ce), 920001 (demo2_m1), 920003 (demo2_m3), "
          "0 (manual). Any OTHER number here, or a magic that doesn't match the account you queried, "
          "is worth investigating as a possible cross-account mixup.")


if __name__ == "__main__":
    main()
