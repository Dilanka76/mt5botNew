"""Checks REAL FORWARD performance of the two entry-quality filters
already validated in backtest (scripts/simulate_demo3_entry_filter.py:
color +$122.58 on demo1_m1, volume +$151.02 on demo1_m3) against the
shadow-logged data that's been accumulating live since these fields were
added to trade_entered log lines (see project_demo3_entryfilter_research
memory) -- these are the most mature, longest-running shadow signals in
the project (longer than the newer trend-filter shadow logging), and
the closest to an actual deployable finding if they hold up forward.

M1 legs (demo1_m1, demo2_m1): checked on shadow_closed_in_favor (color).
M3 legs (demo1_m3, demo2_m3): checked on shadow_low_volume (volume).
Only trade_entered lines that actually carry the relevant shadow field
are included -- this naturally scopes to the post-deployment window
without hardcoding per-account deployment dates.

Matches each trade_entered event to its real outcome via
bot.analytics.get_closed_trades_range (direction + closest real fill
price + closest real time -- same style of matching used throughout this
project's analysis scripts).

    python scripts/check_shadow_filter_forward_results.py

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

M1_ACCOUNTS = ["demo1_m1", "demo2_m1"]
M3_ACCOUNTS = ["demo1_m3", "demo2_m3"]
MATCH_WINDOW = timedelta(minutes=5)


def read_shadow_entries(account: str, field: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "trade_entered" or field not in e:
            continue
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        entries.append(e)
    return entries


def fetch_real_trades(account: str, since: datetime, to: datetime) -> list[dict]:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = mt5_utc_offset(connector, config.symbol)
        trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
    finally:
        connector.disconnect()
    return trades


def match(entries: list[dict], trades: list[dict]) -> list[tuple[dict, dict]]:
    used = set()
    matched = []
    for e in entries:
        best_i, best_score = None, None
        for i, t in enumerate(trades):
            if i in used or t["direction"] != e["direction"]:
                continue
            t_entry_utc = t["entry_time"].astimezone(timezone.utc)
            if abs((t_entry_utc - e["_ts"]).total_seconds()) > MATCH_WINDOW.total_seconds():
                continue
            price_diff = abs(t["entry_price"] - e["entry"])
            if price_diff > 0.10:
                continue
            score = price_diff
            if best_score is None or score < best_score:
                best_i, best_score = i, score
        if best_i is not None:
            used.add(best_i)
            matched.append((e, trades[best_i]))
    return matched


def report(account: str, field: str, label_true: str, label_false: str) -> None:
    entries = read_shadow_entries(account, field)
    if not entries:
        print(f"{account}: no shadow-logged entries with '{field}' found.\n")
        return
    since = min(e["_ts"] for e in entries) - timedelta(minutes=1)
    to = datetime.now(timezone.utc)
    trades = fetch_real_trades(account, since, to)
    pairs = match(entries, trades)

    print(f"{'=' * 70}\n{account}: {len(pairs)}/{len(entries)} shadow-logged entries matched to real "
          f"outcomes (since {since.strftime('%Y-%m-%d %H:%M')} UTC)\n{'=' * 70}")

    for flag_value, label in [(True, label_true), (False, label_false)]:
        bucket = [t for e, t in pairs if e[field] == flag_value]
        if not bucket:
            print(f"  {label}: 0 trades")
            continue
        wins = sum(1 for t in bucket if t["profit"] > 0)
        total = sum(t["profit"] for t in bucket)
        print(f"  {label}: {len(bucket)} trades, {wins} wins "
              f"({100 * wins / len(bucket):.1f}%), P/L ${total:+.2f}")
    print()


def main() -> None:
    print("### M1 legs: color filter (shadow_closed_in_favor) ###\n")
    for account in M1_ACCOUNTS:
        report(account, "shadow_closed_in_favor", "Closed IN favor (agreed)", "Closed AGAINST favor (disagreed)")

    print("### M3 legs: volume filter (shadow_low_volume) ###\n")
    for account in M3_ACCOUNTS:
        report(account, "shadow_low_volume", "LOW volume (bottom third)", "NOT low volume")


if __name__ == "__main__":
    main()
