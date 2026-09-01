"""Systematically compares demo1's swap-reversal trades against demo2's
matching real trades on the SAME underlying signal, to answer: does
demo1's 2-candle+ADX debounce (vs demo2's immediate swap) actually cost
more than it saves? Built 2026-09-01 after a hand-picked, 4-example look
at one day's real trades suggested it might -- this replaces that
small-sample look with a real, matched-pair comparison across a longer
window, same discipline as every other finding in this project.

    python scripts/simulate_swap_debounce_cost.py --since "2026-08-25 00:00:00"

Matching is by (direction, entry time within a tolerance window) -- same
approach as scripts/diff_trades_today.py, since demo1_m1/demo2_m1 (and
demo1_m3/demo2_m3) react to the exact same real candle closes.

Comparison is done PER-LOT (profit / volume) rather than raw dollars,
since demo1's account balance (and therefore lot size) differs from
demo2's -- a raw dollar comparison would be misleading (see
project_demo1_demo2_comparison_log memory's standing caveat about this).

Read-only: connects to MT5 only to read real closed-trade history and
decisions.jsonl, never touches live/demo trading.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config
from bot.mt5_connector import MT5Connector

LEG_PAIRS = [("demo1_m1", "demo2_m1", timedelta(minutes=5)), ("demo1_m3", "demo2_m3", timedelta(minutes=10))]


def parse_args():
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def read_decisions(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    entries = []
    if not path.exists():
        return entries
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = datetime.fromisoformat(e["timestamp"])
                entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda e: e["_ts"])
    return entries


def fetch_trades(account: str, since: datetime, to: datetime) -> list[dict]:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = mt5_utc_offset(connector, config.symbol)
        trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
    finally:
        connector.disconnect()
    return [t for t in trades if t["entry_time"].astimezone(timezone.utc) >= since]


def was_debounce_involved(decisions: list[dict], entry_time: datetime, exit_time: datetime) -> bool:
    """True if demo1's swap_pending (tightened-stop) mechanism fired at
    any point during this specific position's life -- i.e. this trade
    actually went through the debounce path, not just a plain full-stop
    or take-profit close."""
    return any(
        e.get("action") == "swap_pending" and entry_time <= e["_ts"] <= exit_time + timedelta(seconds=5)
        for e in decisions
    )


def match_trades(a: list[dict], b: list[dict], window: timedelta):
    b_used = set()
    matched = []
    for ra in a:
        best_idx, best_delta = None, None
        for i, rb in enumerate(b):
            if i in b_used or rb["direction"] != ra["direction"]:
                continue
            delta = abs((ra["entry_time"] - rb["entry_time"]).total_seconds())
            if delta <= window.total_seconds() and (best_delta is None or delta < best_delta):
                best_idx, best_delta = i, delta
        if best_idx is not None:
            b_used.add(best_idx)
            matched.append((ra, b[best_idx]))
    return matched


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)

    grand_debounce_per_lot = 0.0
    grand_n = 0

    for demo1_acct, demo2_acct, window in LEG_PAIRS:
        d1_trades = fetch_trades(demo1_acct, since, to)
        d2_trades = fetch_trades(demo2_acct, since, to)
        d1_decisions = read_decisions(demo1_acct)

        matched = match_trades(d1_trades, d2_trades, window)

        print(f"{'=' * 78}\n{demo1_acct} vs {demo2_acct}: {len(matched)} matched trades "
              f"(of {len(d1_trades)} demo1 / {len(d2_trades)} demo2), since {args.since}\n{'=' * 78}")

        debounce_pairs = []
        for d1, d2 in matched:
            if was_debounce_involved(d1_decisions, d1["entry_time"].astimezone(timezone.utc),
                                      d1["exit_time"].astimezone(timezone.utc)):
                debounce_pairs.append((d1, d2))

        if not debounce_pairs:
            print("  No debounce-involved matched trades in this window.\n")
            continue

        total_per_lot_diff = 0.0
        for d1, d2 in debounce_pairs:
            d1_per_lot = d1["profit"] / d1["volume"] if d1["volume"] else 0.0
            d2_per_lot = d2["profit"] / d2["volume"] if d2["volume"] else 0.0
            diff = d1_per_lot - d2_per_lot  # negative = demo1's debounce cost more (per lot) than demo2's immediate swap
            total_per_lot_diff += diff
            entry_local = d1["entry_time"].strftime("%H:%M:%S")
            print(f"  [{entry_local}] {d1['direction']}: demo1 ${d1['profit']:+.2f}/{d1['volume']:.2f}lot="
                  f"${d1_per_lot:+.2f}/lot  vs  demo2 ${d2['profit']:+.2f}/{d2['volume']:.2f}lot=${d2_per_lot:+.2f}/lot  "
                  f"-> diff ${diff:+.2f}/lot")

        avg_diff = total_per_lot_diff / len(debounce_pairs)
        print(f"\n  {len(debounce_pairs)} debounce-involved matched trades: "
              f"total ${total_per_lot_diff:+.2f}/lot, avg ${avg_diff:+.2f}/lot per trade")
        print(f"  -> demo1's debounce+ADX gate {'COST' if total_per_lot_diff < 0 else 'SAVED'} "
              f"${abs(total_per_lot_diff):.2f}/lot combined vs demo2's immediate swap on these trades\n")

        grand_debounce_per_lot += total_per_lot_diff
        grand_n += len(debounce_pairs)

    if grand_n:
        print(f"{'=' * 78}\nGRAND TOTAL: {grand_n} debounce-involved matched trades across both legs, "
              f"${grand_debounce_per_lot:+.2f}/lot combined\n{'=' * 78}")


if __name__ == "__main__":
    main()
