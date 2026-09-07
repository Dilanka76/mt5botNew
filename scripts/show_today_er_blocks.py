"""Today's demo1 entries with their shadow Efficiency Ratio: how many
would ER have blocked, and would that have helped?

ER IS NOT LIVE. It has never blocked a single trade on any account. The
engines compute it and write it to decisions.jsonl as
shadow_er_close/shadow_er_truerange, purely to accumulate forward
evidence at zero risk. This script answers the hypothetical: if ER had
been switched on today at threshold X, which of today's entries would it
have stopped, and what did those trades actually do?

Reads the shadow values the ENGINE logged at decision time, so it does
not depend on after-the-fact candle matching at all -- the bug that
invalidated a week of research (see bot/strategy/cross_lookup.py) cannot
affect these numbers. MT5 is used only to look up each ticket's realised
P/L.

    python scripts/show_today_er_blocks.py
    python scripts/show_today_er_blocks.py --date 2026-09-05
    python scripts/show_today_er_blocks.py --offset-hours 3   # market closed

TWO LIMITATIONS, both real:
  - Single-trade counterfactual. Had a blocked trade not been taken, the
    bot would have been flat and later signals would have played out
    differently. This answers "was each skip right?", not "what would
    the balance be?".
  - One day is a tiny sample. Use it to see what ER is doing, not to
    decide anything. The full walk-forward test is
    scripts/analyze_efficiency_ratio.py.

Read-only: never places or modifies a trade.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
THRESHOLDS = [0.05, 0.10, 0.15, 0.20]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    p.add_argument("--date", default=None, help="YYYY-MM-DD Colombo day (default: today)")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker UTC offset; supply manually when the market is closed "
                        "(a stale tick otherwise reports its own age -- see "
                        "project_stale_tick_offset_bug)")
    return p.parse_args()


def read_entries(account: str, start: datetime, end: datetime) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    rows = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if e.get("action") != "trade_entered" or not (start <= ts < end):
                continue
            e["_ts"] = ts
            rows.append(e)
    rows.sort(key=lambda e: e["_ts"])
    return rows


MATCH_WINDOW_SECONDS = 180
PRICE_TOLERANCE = 1.0


def match_entries_to_trades(entries: list[dict], trades: list[dict]) -> None:
    """Pair each logged trade_entered with its real MT5 trade.

    trade_entered does NOT log a ticket (see the engine's _enter()), so
    there is no id to join on. Match instead on direction plus entry
    time -- log_decision writes its timestamp immediately after
    order_send() returns, so the two are seconds apart -- and require
    the entry price to agree as a sanity check. Matching is one-to-one:
    each real trade can claim at most one logged entry, so two entries
    seconds apart cannot both bind to the same trade.

    Sets e["_profit"] to the realised P/L, or None when nothing matched
    (a still-open position, or a trade that closed after the window).
    """
    used: set[int] = set()
    for e in entries:
        best, best_gap = None, None
        for i, t in enumerate(trades):
            if i in used:
                continue
            if str(t.get("direction")) != str(e.get("direction")):
                continue
            gap = abs((t["entry_time"].astimezone(timezone.utc) - e["_ts"]).total_seconds())
            if gap > MATCH_WINDOW_SECONDS:
                continue
            if abs(float(t["entry_price"]) - float(e.get("entry", 0))) > PRICE_TOLERANCE:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = i, gap
        if best is None:
            e["_profit"] = None
        else:
            used.add(best)
            e["_profit"] = trades[best]["profit"]


def summarise(label: str, rows: list[dict], key: str) -> None:
    """For each threshold: how many of today's entries ER would have
    stopped, and what those specific trades actually did."""
    have = [r for r in rows if r.get(key) is not None and r.get("_profit") is not None]
    if not have:
        print(f"    {label}: no entries carry both a {key} value and a matched P/L.")
        return
    print(f"    {label} (over the {len(have)} entries with a known outcome):")
    for th in THRESHOLDS:
        blocked = [r for r in have if r[key] < th]
        if not blocked:
            print(f"      >= {th:.2f}  would block  0")
            continue
        pl = sum(r["_profit"] for r in blocked)
        wins = sum(1 for r in blocked if r["_profit"] > 0)
        # Blocking trades that lost money is a saving; blocking winners costs.
        verdict = "HELPED" if pl < 0 else "COST"
        print(f"      >= {th:.2f}  would block {len(blocked):2d} of {len(have)}  "
              f"({wins} of them won)  their actual P/L ${pl:+.2f}  -> would have "
              f"{verdict} ${abs(pl):.2f}")


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(COLOMBO).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=COLOMBO).astimezone(timezone.utc)
    end = min(start + timedelta(days=1), datetime.now(timezone.utc))

    print(f"Colombo day {day.isoformat()}   (ER is SHADOW-ONLY -- nothing below was actually blocked)\n")

    for account in accounts:
        config = load_config(account)
        entries = read_entries(account, start, end)

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, start, end, offset)
        finally:
            connector.disconnect()

        match_entries_to_trades(entries, trades)

        print("=" * 86)
        matched = sum(1 for e in entries if e["_profit"] is not None)
        print(f"{account} ({config.timeframe}): {len(entries)} logged entries today, "
              f"{len(trades)} closed trades in MT5, {matched} paired")
        if entries and matched < len(entries) - 1:
            # One unpaired entry is normal (the position still open). More
            # than that means the pairing itself is failing -- say so loudly
            # rather than quietly reporting them as "open".
            print(f"  WARNING: {len(entries) - matched} entries did not pair with an MT5 trade. "
                  f"Treat the threshold table below as incomplete.")
        print("=" * 86)
        if not entries:
            print("  No entries today.\n")
            continue

        print(f"  {'time (Colombo)':<17}{'dir':<6}{'ER close':>10}{'ER true':>10}{'P/L':>12}")
        for e in entries:
            erc = e.get("shadow_er_close")
            ert = e.get("shadow_er_truerange")
            pl = e["_profit"]
            print(f"  {e['_ts'].astimezone(COLOMBO).strftime('%H:%M:%S'):<17}"
                  f"{str(e.get('direction', '?')):<6}"
                  f"{(f'{erc:.4f}' if erc is not None else 'n/a'):>10}"
                  f"{(f'{ert:.4f}' if ert is not None else 'n/a'):>10}"
                  f"{(f'${pl:+.2f}' if pl is not None else 'open'):>12}")
        print()
        summarise("close-only ER", entries, "shadow_er_close")
        summarise("true-range ER", entries, "shadow_er_truerange")
        print()

    print("Reminder: single-trade counterfactuals, and one day is far too small to")
    print("decide anything. ER passed only 4 of 32 walk-forward tests (chance predicts")
    print("~8) -- see scripts/analyze_efficiency_ratio.py.")


if __name__ == "__main__":
    main()
