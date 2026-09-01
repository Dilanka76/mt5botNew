"""Replays demo1's REAL trade history against two session-window
hypotheses from 2026-09-01's time-of-day research (the strongest,
most-consistent finding of that research -- positive in ALL 4 accounts
independently for the 00:00-04:00 broker/app-time window, negative in 3
of 4 for 08:00-12:00): what would the real dollar difference have been
if trading (a) excluded the worst window, or (b) was limited to only
the best window?

    python scripts/simulate_time_of_day_window.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

"Broker/app time" = UTC+3, the same clock shown directly on the MT5
terminal (see bot.analytics.mt5_utc_offset) -- NOT Sri Lanka time.
get_closed_trades_range() already returns entry_time as a proper
tz-aware datetime (Colombo-labeled), so converting it to broker/app time
needs no live MT5 connection for a fresh offset measurement -- just a
correct timezone conversion through true UTC.

Read-only: connects to MT5 only to read real closed-trade history, never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

APP_TZ = timezone(timedelta(hours=3))
EXCLUDE_WINDOW = (8, 12)   # broker/app-time hours, worst window per the research
ONLY_WINDOW = (0, 4)       # broker/app-time hours, best window per the research


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    combined_actual = 0.0
    combined_exclude = 0.0
    combined_only = 0.0

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
            app_hour = entry_utc.astimezone(APP_TZ).hour
            rows.append({"profit": t["profit"], "app_hour": app_hour})

        if not rows:
            print(f"{account}: no trades in this window.\n")
            continue

        actual_total = sum(r["profit"] for r in rows)
        actual_wins = sum(1 for r in rows if r["profit"] > 0)

        excluded = [r for r in rows if not (EXCLUDE_WINDOW[0] <= r["app_hour"] < EXCLUDE_WINDOW[1])]
        excl_total = sum(r["profit"] for r in excluded)
        excl_wins = sum(1 for r in excluded if r["profit"] > 0)

        only = [r for r in rows if ONLY_WINDOW[0] <= r["app_hour"] < ONLY_WINDOW[1]]
        only_total = sum(r["profit"] for r in only)
        only_wins = sum(1 for r in only if r["profit"] > 0)

        print(f"{'=' * 70}\n{account}: {len(rows)} real trades, since {args.since}\n{'=' * 70}")
        print(f"ACTUAL (all trades):                    {len(rows)} trades, {actual_wins} wins "
              f"({100 * actual_wins / len(rows):.1f}%), total P/L ${actual_total:+.2f}")
        print(f"EXCLUDE 08:00-12:00 broker time (worst): {len(excluded)} trades, {excl_wins} wins "
              f"({100 * excl_wins / len(excluded) if excluded else 0:.1f}%), total P/L ${excl_total:+.2f} "
              f"-> {'ADDED' if excl_total > actual_total else 'COST'} ${abs(excl_total - actual_total):.2f}")
        print(f"ONLY 00:00-04:00 broker time (best):     {len(only)} trades, {only_wins} wins "
              f"({100 * only_wins / len(only) if only else 0:.1f}%), total P/L ${only_total:+.2f} "
              f"-> {'ADDED' if only_total > actual_total else 'COST'} ${abs(only_total - actual_total):.2f} "
              f"(trades cut by {100 * (1 - len(only) / len(rows)):.0f}%)\n")

        combined_actual += actual_total
        combined_exclude += excl_total
        combined_only += only_total

    if len(accounts) > 1:
        print(f"{'=' * 70}\nCOMBINED across {', '.join(accounts)}\n{'=' * 70}")
        print(f"ACTUAL:                    ${combined_actual:+.2f}")
        print(f"EXCLUDE 08:00-12:00:       ${combined_exclude:+.2f}  "
              f"({'ADDED' if combined_exclude > combined_actual else 'COST'} "
              f"${abs(combined_exclude - combined_actual):.2f})")
        print(f"ONLY 00:00-04:00:          ${combined_only:+.2f}  "
              f"({'ADDED' if combined_only > combined_actual else 'COST'} "
              f"${abs(combined_only - combined_actual):.2f})")


if __name__ == "__main__":
    main()
