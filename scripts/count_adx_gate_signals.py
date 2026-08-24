"""Counts every confirmed EMA13/21 cross since a given timestamp for
dual_cross_confirmed_adx_m15 (ADX-only entry gate, live since
2026-08-23), splitting them into "took the trade" vs "blocked by low
ADX" vs "blocked, outside session" — so real numbers can answer "how
many trades did the ADX filter actually skip, and how close were they."

A confirmed cross always produces exactly one of these decisions.jsonl
actions:
  - cross_ignored_outside_session -> blocked, no session open
  - entry_blocked_low_adx          -> blocked, ADX below threshold
  - setup_pending                  -> passed the gate, gap too wide for
                                       immediate entry, waiting for EMA5
  - trade_entered (reason starts "close-confirmed: candle closed")
                                    -> passed the gate, immediate entry

A later "trade_entered (reason starts "EMA5 touch at")" is just the FILL
of an earlier setup_pending — not a new signal, so it's not counted
again here (it would double-count otherwise). "pending_cancelled" is
also not counted as its own signal — the cross that cancelled the old
pending is itself a fresh signal captured by one of the four actions
above.

    python scripts/count_adx_gate_signals.py --accounts demo1_m1,demo1_m3 --since "2026-08-23 22:42:59"

Reads decisions.jsonl only — no MT5 connection needed.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ADX_RE = re.compile(r"adx=([\d.]+|nan)")


def load_entries(account: str, since: datetime) -> list[tuple[datetime, dict]]:
    path = Path(f"logs/{account}/decisions.jsonl")
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            ts = datetime.fromisoformat(e["timestamp"])
            if ts < since:
                continue
            entries.append((ts, e))
    return entries


def classify(entries: list[tuple[datetime, dict]]) -> dict:
    taken = []      # (ts, adx, kind)
    blocked_adx = []  # (ts, adx)
    blocked_session = []  # (ts,)

    for ts, e in entries:
        action = e.get("action")
        reason = e.get("reason", "")

        if action == "entry_blocked_low_adx":
            m = ADX_RE.search(reason)
            adx = m.group(1) if m else "?"
            blocked_adx.append((ts, adx))
        elif action == "cross_ignored_outside_session":
            blocked_session.append((ts,))
        elif action == "setup_pending":
            m = ADX_RE.search(reason)
            adx = m.group(1) if m else "?"
            taken.append((ts, adx, "pending"))
        elif action == "trade_entered" and reason.startswith("close-confirmed: candle closed"):
            m = ADX_RE.search(reason)
            adx = m.group(1) if m else "?"
            taken.append((ts, adx, "immediate"))

    return {"taken": taken, "blocked_adx": blocked_adx, "blocked_session": blocked_session}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3", help="Comma-separated account names")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", UTC')
    args = parser.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    accounts = args.accounts.split(",")

    combined_taken = 0
    combined_blocked_adx = 0
    combined_blocked_session = 0

    for account in accounts:
        entries = load_entries(account, since)
        result = classify(entries)
        taken, blocked_adx, blocked_session = result["taken"], result["blocked_adx"], result["blocked_session"]
        total_signals = len(taken) + len(blocked_adx) + len(blocked_session)

        print("=" * 70)
        print(f"ACCOUNT: {account}  (since {args.since} UTC)")
        print("=" * 70)
        print(f"Total confirmed crosses (signals): {total_signals}")
        print(f"  Taken (passed ADX gate):      {len(taken)}")
        print(f"  Blocked by low ADX:           {len(blocked_adx)}")
        print(f"  Blocked, outside session:     {len(blocked_session)}")
        print()

        if taken:
            print("  Taken signals:")
            for ts, adx, kind in taken:
                print(f"    {ts.isoformat()}  adx={adx:>5}  ({kind})")
        if blocked_adx:
            print("\n  Blocked by low ADX:")
            for ts, adx in blocked_adx:
                print(f"    {ts.isoformat()}  adx={adx:>5}")
        if blocked_session:
            print("\n  Blocked, outside session:")
            for (ts,) in blocked_session:
                print(f"    {ts.isoformat()}")
        print()

        combined_taken += len(taken)
        combined_blocked_adx += len(blocked_adx)
        combined_blocked_session += len(blocked_session)

    combined_total = combined_taken + combined_blocked_adx + combined_blocked_session
    print("=" * 70)
    print(f"COMBINED (all accounts): {combined_total} total confirmed crosses")
    print("=" * 70)
    print(f"  Taken (passed ADX gate):      {combined_taken}")
    print(f"  Blocked by low ADX:           {combined_blocked_adx}")
    print(f"  Blocked, outside session:     {combined_blocked_session}")


if __name__ == "__main__":
    main()
