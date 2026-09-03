"""Splits demo1's debounce+ADX swap-gate cost (see
scripts/simulate_swap_debounce_cost.py) into its TWO separate
mechanisms, to answer: if the recent window is a real cost, which piece
is actually causing it -- the 2-candle debounce delay, or the ADX>=25
threshold specifically?

Motivation (2026-09-03): simulate_swap_debounce_cost.py bundles both
together (anything where swap_pending fired at all counts as
"debounce-involved"), and the recent window (since 2026-09-01) showed a
real reversal -- demo1's combined mechanism COST $1252.00/lot vs the
full-period result of SAVING $1520.00/lot. Before touching live config
(user proposed possibly removing "the ADX part"), this decomposes WHY,
using signals already logged, no new instrumentation:

  - If `swap_blocked_low_adx` fired at least once during a position's
    life: the ADX threshold ACTIVELY blocked a confirmed 2-candle
    reversal at least once -- bucketed as "ADX-blocked".
  - If `swap_pending` fired but `swap_blocked_low_adx` never did: the
    position's fate was governed purely by the 2-candle debounce timing
    (either it reconfirmed and swapped one candle late, or the pending
    reversal was cancelled because the very next candle didn't
    reconfirm) -- bucketed as "debounce-only", ADX never came into play.

Same matching/per-lot methodology as simulate_swap_debounce_cost.py
(reused, not imported, to keep each script's baseline self-contained --
same convention as every other analysis script in this project).

    python scripts/decompose_debounce_adx_cost.py --since "2026-08-25 00:00:00"
    python scripts/decompose_debounce_adx_cost.py --since "2026-09-01 00:00:00"

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


def classify_debounce_involvement(decisions: list[dict], entry_time: datetime, exit_time: datetime) -> str | None:
    """Returns "adx_blocked", "debounce_only", or None (debounce never
    engaged at all for this position's life)."""
    window_end = exit_time + timedelta(seconds=5)
    saw_pending = False
    saw_adx_block = False
    for e in decisions:
        if not (entry_time <= e["_ts"] <= window_end):
            continue
        if e.get("action") == "swap_pending":
            saw_pending = True
        elif e.get("action") == "swap_blocked_low_adx":
            saw_adx_block = True
    if saw_adx_block:
        return "adx_blocked"
    if saw_pending:
        return "debounce_only"
    return None


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

    grand = {"adx_blocked": [0.0, 0], "debounce_only": [0.0, 0]}

    for demo1_acct, demo2_acct, window in LEG_PAIRS:
        d1_trades = fetch_trades(demo1_acct, since, to)
        d2_trades = fetch_trades(demo2_acct, since, to)
        d1_decisions = read_decisions(demo1_acct)

        matched = match_trades(d1_trades, d2_trades, window)

        buckets: dict[str, list[tuple[dict, dict]]] = {"adx_blocked": [], "debounce_only": []}
        for d1, d2 in matched:
            kind = classify_debounce_involvement(
                d1_decisions, d1["entry_time"].astimezone(timezone.utc), d1["exit_time"].astimezone(timezone.utc),
            )
            if kind is not None:
                buckets[kind].append((d1, d2))

        print(f"{'=' * 78}\n{demo1_acct} vs {demo2_acct}: {len(matched)} matched trades, since {args.since}\n{'=' * 78}")

        for kind, label in [("adx_blocked", "ADX-blocked (threshold actively stopped a confirmed reversal)"),
                             ("debounce_only", "Debounce-only (2-candle wait, ADX never blocked anything)")]:
            pairs = buckets[kind]
            if not pairs:
                print(f"  {label}: 0 matched trades\n")
                continue
            total = 0.0
            for d1, d2 in pairs:
                d1_per_lot = d1["profit"] / d1["volume"] if d1["volume"] else 0.0
                d2_per_lot = d2["profit"] / d2["volume"] if d2["volume"] else 0.0
                diff = d1_per_lot - d2_per_lot
                total += diff
                print(f"    [{d1['entry_time'].strftime('%H:%M:%S')}] {d1['direction']}: "
                      f"demo1 ${d1_per_lot:+.2f}/lot vs demo2 ${d2_per_lot:+.2f}/lot -> diff ${diff:+.2f}/lot")
            avg = total / len(pairs)
            print(f"  {label}: {len(pairs)} trades, total ${total:+.2f}/lot, avg ${avg:+.2f}/lot "
                  f"-> {'COST' if total < 0 else 'SAVED'} ${abs(total):.2f}/lot\n")
            grand[kind][0] += total
            grand[kind][1] += len(pairs)

    print(f"{'=' * 78}\nGRAND TOTAL (all legs)\n{'=' * 78}")
    for kind, label in [("adx_blocked", "ADX-blocked"), ("debounce_only", "Debounce-only")]:
        total, n = grand[kind]
        if n:
            verdict = "COST" if total < 0 else "SAVED"
            print(f"  {label}: {n} trades, ${total:+.2f}/lot combined -> {verdict} ${abs(total):.2f}/lot")
        else:
            print(f"  {label}: 0 trades")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    main()
