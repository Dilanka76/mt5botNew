"""Prints a side-by-side today's-trades comparison between two account
pairs (default: demo1 vs demo2) -- built 2026-08-27 to answer "how did
demo1 and demo2 compare today" without needing to open either Google
Sheets report (which are private, not fetchable directly).

Reuses generate_live_test_report.py's gather_account_data()/trading_day()
unchanged so this always matches whatever the reports themselves show --
same 03:30-to-03:30 Sri Lanka day boundary, same MT5-offset-corrected real
trade data.

    python scripts/compare_today.py
    python scripts/compare_today.py --pair-a demo1_m1:M1,demo1_m3:M3 --pair-b demo2_m1:M1,demo2_m3:M3

Read-only: only pulls real closed-trade history, never touches live/demo
trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so generate_live_test_report itself is importable

from generate_live_test_report import gather_account_data, trading_day  # noqa: E402


def parse_pair(spec: str) -> list[tuple[str, str]]:
    """"demo1_m1:M1,demo1_m3:M3" -> [("demo1_m1", "M1"), ("demo1_m3", "M3")]"""
    legs = []
    for part in spec.split(","):
        account, timeframe = part.split(":")
        legs.append((account.strip(), timeframe.strip()))
    return legs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pair-a", default="demo1_m1:M1,demo1_m3:M3")
    parser.add_argument("--pair-b", default="demo2_m1:M1,demo2_m3:M3")
    return parser.parse_args()


def today_records(legs: list[tuple[str, str]], today) -> tuple[list[dict], dict]:
    all_records: list[dict] = []
    by_leg: dict[str, list[dict]] = {}
    for account, timeframe in legs:
        print(f"  Reading {account} ({timeframe})...")
        _decisions, records, _rules = gather_account_data(account, timeframe)
        leg_today = [r for r in records if trading_day(r["exit_time"]) == today]
        by_leg[account] = leg_today
        all_records.extend(leg_today)
    return all_records, by_leg


def summarize(label: str, records: list[dict], by_leg: dict[str, list[dict]]) -> None:
    n = len(records)
    wins = [r for r in records if r["outcome"] == "WIN"]
    pl = sum(r["profit"] for r in records)
    win_rate = 100 * len(wins) / n if n else 0.0
    avg_win = mean([r["profit"] for r in wins]) if wins else 0.0
    losses = [r for r in records if r["outcome"] == "LOSS"]
    avg_loss = mean([r["profit"] for r in losses]) if losses else 0.0

    print(f"\n{label}")
    print(f"  Trades: {n}  |  Win rate: {win_rate:.1f}%  |  Total P/L: ${pl:+.2f}")
    print(f"  Avg win: ${avg_win:+.2f}  |  Avg loss: ${avg_loss:+.2f}")
    for account, leg_records in by_leg.items():
        leg_pl = sum(r["profit"] for r in leg_records)
        leg_wins = sum(1 for r in leg_records if r["outcome"] == "WIN")
        leg_wr = 100 * leg_wins / len(leg_records) if leg_records else 0.0
        print(f"    {account}: {len(leg_records)} trades, {leg_wr:.1f}% win, ${leg_pl:+.2f}")


def main() -> None:
    args = parse_args()
    legs_a = parse_pair(args.pair_a)
    legs_b = parse_pair(args.pair_b)

    today = trading_day(datetime.now(timezone.utc))
    print(f"Today's trading day (03:30-to-03:30 Sri Lanka time): {today.isoformat()}\n")

    print(f"{'=' * 70}\nGathering {args.pair_a}...\n{'=' * 70}")
    records_a, by_leg_a = today_records(legs_a, today)
    print(f"\n{'=' * 70}\nGathering {args.pair_b}...\n{'=' * 70}")
    records_b, by_leg_b = today_records(legs_b, today)

    print(f"\n{'=' * 70}\nTODAY'S COMPARISON — {today.isoformat()}\n{'=' * 70}")
    summarize(args.pair_a, records_a, by_leg_a)
    summarize(args.pair_b, records_b, by_leg_b)

    print(f"\n{'=' * 70}")
    pl_a, pl_b = sum(r["profit"] for r in records_a), sum(r["profit"] for r in records_b)
    if records_a or records_b:
        leader = args.pair_a if pl_a > pl_b else (args.pair_b if pl_b > pl_a else None)
        if leader:
            print(f"Ahead today (by P/L): {leader}  (${abs(pl_a - pl_b):.2f} difference)")
        else:
            print("Tied on P/L today.")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
