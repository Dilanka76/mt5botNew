"""Trade-by-trade replay testing two ENTRY filters that no other research
in this project has examined: rules based on the PATTERN OF RECENT
SIGNALS rather than any characteristic of the confirming candle itself.

Motivation (2026-09-03): every entry-quality point checked so far (the
"7 points" -- candle color, tick volume, EMA13/21 separation, ATR,
time-of-day, candle decisiveness, gap-from-EMA13; see
project_demo3_entryfilter_research) measures ONE candle in isolation.
But real losses cluster tightly in time -- demo2_m1 on 2026-09-03 lost
on entries at 04:40, 04:44, 05:53, 05:56 and 06:01 Colombo; demo1_m1 on
2026-09-02 lost at 04:21, 05:01, 05:48, 05:59, 06:15, 06:47. That is the
whipsaw signature: when the market chops, EMA13/21 crosses back and
forth and the engine takes every one. A single-candle filter is
structurally blind to it.

Two rules tested, both "should this entry be taken", both using only
information available BEFORE the entry:

  1. SPACING: skip an entry that comes less than N minutes after the
     previous TAKEN entry (tested at several N).
  2. LOSS COOLDOWN: after N consecutive losing TAKEN trades, skip
     entries until a cooldown period passes (tested at N=1, 2).

Simulation honesty note: when a rule skips a trade, that trade never
happens, so it cannot contribute to a later loss streak or reset the
spacing clock -- the replay tracks the counterfactual state (last TAKEN
entry, streak of TAKEN losses), not the original real sequence. This
matters: a naive version that keeps using the real sequence's outcomes
would be using information the rule itself would have prevented from
existing.

Walk-forward split included (first half of real trades by time vs
second half) -- a rule only counts if both halves agree, same discipline
as scripts/analyze_trend_filter.py.

    python scripts/simulate_signal_clustering_filter.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read real closed-trade history, never
touches live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import validate_account_name, load_config
from bot.mt5_connector import MT5Connector

SPACING_MINUTES = [5, 10, 15, 30]
COOLDOWN_SPECS = [(1, 15), (1, 30), (2, 15), (2, 30), (2, 60)]  # (consecutive losses, cooldown minutes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def apply_spacing(trades: list[dict], min_minutes: int) -> list[dict]:
    """Keep a trade only if it starts at least min_minutes after the
    previous KEPT trade's entry (skipped trades never happened, so they
    don't reset the clock)."""
    kept = []
    last_kept_entry: datetime | None = None
    for t in trades:
        if last_kept_entry is None or (t["_entry"] - last_kept_entry) >= timedelta(minutes=min_minutes):
            kept.append(t)
            last_kept_entry = t["_entry"]
    return kept


def apply_cooldown(trades: list[dict], streak_len: int, cooldown_minutes: int) -> list[dict]:
    """After `streak_len` consecutive LOSING kept trades, skip every
    entry until `cooldown_minutes` have passed since that last losing
    trade closed. Streak counts only trades that were actually taken."""
    kept = []
    streak = 0
    blocked_until: datetime | None = None
    for t in trades:
        if blocked_until is not None and t["_entry"] < blocked_until:
            continue  # still in cooldown -- entry never taken
        blocked_until = None
        kept.append(t)
        if t["profit"] <= 0:
            streak += 1
            if streak >= streak_len:
                blocked_until = t["_exit"] + timedelta(minutes=cooldown_minutes)
                streak = 0
        else:
            streak = 0
    return kept


def report(label: str, original: list[dict], kept: list[dict]) -> None:
    if not original:
        print(f"    {label}: no trades in this slice.")
        return
    actual_total = sum(t["profit"] for t in original)
    kept_total = sum(t["profit"] for t in kept)
    kept_wins = sum(1 for t in kept if t["profit"] > 0)
    skipped_n = len(original) - len(kept)
    diff = kept_total - actual_total
    wr = 100 * kept_wins / len(kept) if kept else 0.0
    print(f"    {label}: keep {len(kept)}/{len(original)} ({wr:.1f}% win), P/L ${kept_total:+.2f} "
          f"vs actual ${actual_total:+.2f} -> {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f} "
          f"(skipped {100 * skipped_n / len(original):.0f}%)")


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
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
        finally:
            connector.disconnect()

        trades = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            trades.append({**t, "_entry": entry_utc, "_exit": t["exit_time"].astimezone(timezone.utc)})
        if not trades:
            print(f"{account}: no trades in this window.\n")
            continue
        trades.sort(key=lambda t: t["_entry"])

        mid = len(trades) // 2
        halves = [("Full sample", trades), ("First half", trades[:mid]), ("Second half", trades[mid:])]

        actual_total = sum(t["profit"] for t in trades)
        actual_wins = sum(1 for t in trades if t["profit"] > 0)
        print(f"{'=' * 74}\n{account}: {len(trades)} real trades, {actual_wins} wins "
              f"({100 * actual_wins / len(trades):.1f}%), actual P/L ${actual_total:+.2f}\n{'=' * 74}")

        print("  SIGNAL SPACING (skip an entry too soon after the previous taken one):")
        for minutes in SPACING_MINUTES:
            print(f"   -- minimum {minutes} min between entries --")
            for slice_label, slice_trades in halves:
                report(slice_label, slice_trades, apply_spacing(slice_trades, minutes))
        print()

        print("  LOSS COOLDOWN (pause after consecutive losing trades):")
        for streak_len, cooldown in COOLDOWN_SPECS:
            print(f"   -- after {streak_len} consecutive loss(es), pause {cooldown} min --")
            for slice_label, slice_trades in halves:
                report(slice_label, slice_trades, apply_cooldown(slice_trades, streak_len, cooldown))
        print()


if __name__ == "__main__":
    main()
