"""Matches today's real trades between demo1 and demo2 leg-by-leg (m1 vs
m1, m3 vs m3 -- same timeframe, same symbol, same underlying EMA13/21
cross data) to answer: which trades did demo2 take that demo1 didn't, and
vice versa -- built 2026-08-27 at the user's request for this level of
detail beyond compare_today.py's totals-only comparison.

    python scripts/diff_trades_today.py

Matching is by (direction, entry time within a tolerance window) --
demo1_m1/demo2_m1 both react to the exact same real M1 candle closes, so a
trade caused by the SAME underlying signal should open within seconds of
each other on both accounts, UNLESS one of them is already in a different
position (holding from an earlier divergence) or blocked by its own extra
rule (demo1's ADX-gated/2-candle-debounced swap vs demo2's immediate one).
A generous tolerance (5 min M1, 10 min M3) absorbs the debounce/ADX-gate
delay itself, so an "unmatched" trade is a genuine divergence, not just
timing noise.

For every demo2-only trade (demo2 took it, demo1 didn't), also scans
demo1's own decisions.jsonl around that same window for a blocked/pending/
session-related event that explains WHY -- grounds the diff in an actual
logged reason wherever one exists, rather than leaving it unexplained.

Read-only: only pulls real closed-trade history and decisions.jsonl,
never touches live/demo trading.
"""
from __future__ import annotations

import sys
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so generate_live_test_report itself is importable

from datetime import datetime

from generate_live_test_report import gather_account_data, read_decisions, trading_day  # noqa: E402

LEG_PAIRS = [("demo1_m1", "demo2_m1", "M1", timedelta(minutes=5)), ("demo1_m3", "demo2_m3", "M3", timedelta(minutes=10))]

EXPLAIN_ACTIONS = (
    "entry_blocked_adx_falling", "swap_blocked_low_adx", "swap_pending", "pending_cancelled",
    "cross_ignored_outside_session", "pending_touch_outside_session", "setup_pending",
)


def fmt_trade(r: dict) -> str:
    return (
        f"{r['entry_time'].astimezone(timezone.utc).strftime('%H:%M:%S')} UTC  {r['direction']:<4}  "
        f"entry={r['entry_price']:.2f}  exit={r['exit_price']:.2f}  P/L=${r['profit']:+.2f}  "
        f"({r['outcome']}, {r['close_category']})"
    )


def find_nearby_explanation(decisions: list[dict], near: datetime, direction: str, window: timedelta) -> str | None:
    candidates = [
        e for e in decisions
        if e.get("action") in EXPLAIN_ACTIONS
        and abs(e["_ts"] - near) <= window
        and (direction in e.get("reason", "") or direction not in ("BUY", "SELL"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: abs(e["_ts"] - near))
    best = candidates[0]
    return f"{best['action']}: {best['reason']}"


def match_trades(a_records: list[dict], b_records: list[dict], window: timedelta) -> tuple[list[tuple], list[dict], list[dict]]:
    """Greedy nearest-neighbor match by (direction, entry_time within window).
    Returns (matched_pairs, a_unmatched, b_unmatched)."""
    b_used: set[int] = set()
    matched = []
    a_unmatched = []
    for ra in a_records:
        best_idx, best_delta = None, None
        for i, rb in enumerate(b_records):
            if i in b_used or rb["direction"] != ra["direction"]:
                continue
            delta = abs((ra["entry_time"] - rb["entry_time"]).total_seconds())
            if delta <= window.total_seconds() and (best_delta is None or delta < best_delta):
                best_idx, best_delta = i, delta
        if best_idx is not None:
            b_used.add(best_idx)
            matched.append((ra, b_records[best_idx]))
        else:
            a_unmatched.append(ra)
    b_unmatched = [rb for i, rb in enumerate(b_records) if i not in b_used]
    return matched, a_unmatched, b_unmatched


def main() -> None:
    today = trading_day(datetime.now(timezone.utc))
    print(f"Today's trading day (03:30-to-03:30 Sri Lanka time): {today.isoformat()}\n")

    for demo1_account, demo2_account, timeframe, window in LEG_PAIRS:
        print(f"{'=' * 78}\n{demo1_account} vs {demo2_account} ({timeframe})\n{'=' * 78}")

        d1_decisions, d1_records_all, _ = gather_account_data(demo1_account, timeframe)
        d2_decisions, d2_records_all, _ = gather_account_data(demo2_account, timeframe)
        d1_records = [r for r in d1_records_all if trading_day(r["exit_time"]) == today]
        d2_records = [r for r in d2_records_all if trading_day(r["exit_time"]) == today]

        matched, demo1_only, demo2_only = match_trades(d1_records, d2_records, window)

        print(f"\n  MATCHED (both took it, within {int(window.total_seconds() // 60)} min of each other): {len(matched)}")
        for ra, rb in matched:
            print(f"    demo1: {fmt_trade(ra)}")
            print(f"    demo2: {fmt_trade(rb)}")

        print(f"\n  DEMO2 TOOK IT, DEMO1 DID NOT: {len(demo2_only)}")
        for r in demo2_only:
            print(f"    {fmt_trade(r)}")
            explanation = find_nearby_explanation(d1_decisions, r["entry_time"], r["direction"], window)
            if explanation:
                print(f"      -> demo1's likely reason: {explanation}")
            else:
                print(f"      -> no matching blocked/pending event found in demo1's log near this time "
                      f"(demo1 may have simply been in a different position state)")

        print(f"\n  DEMO1 TOOK IT, DEMO2 DID NOT: {len(demo1_only)}")
        for r in demo1_only:
            print(f"    {fmt_trade(r)}")
            explanation = find_nearby_explanation(d2_decisions, r["entry_time"], r["direction"], window)
            if explanation:
                print(f"      -> demo2's likely reason: {explanation}")
            else:
                print(f"      -> no matching blocked/pending event found in demo2's log near this time "
                      f"(demo2 may have simply been in a different position state)")
        print()


if __name__ == "__main__":
    main()
