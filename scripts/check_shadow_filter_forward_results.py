"""Checks REAL FORWARD performance of every shadow-logged entry filter,
on every account, against the real outcome of each trade.

Shadow logging attaches "what would this filter have said" to each real
trade_entered line without ever changing behavior (see
project_demo3_entryfilter_research and project_trend_filter_research).
This script joins those flags to real closed-trade P/L, so a filter's
backtest claim can be checked against what actually happened since it
was deployed.

Reports BOTH buckets per filter with an average P/L PER TRADE -- the
bucket sizes are very unequal (e.g. 50 "agreed" vs 12 "disagreed"), so
comparing bucket totals is misleading; only the per-trade average is a
fair comparison. The verdict line states what filtering would have done.

Every filter is checked on every account (2026-09-04): the earlier
version tested color on M1 legs only and volume on M3 legs only, based
on older demo1-specific findings -- that left real blind spots (notably
no forward data on color for M3, where demo2_m3's backtest looked
strongest).

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

ACCOUNTS = ["demo1_m1", "demo1_m3", "demo2_m1", "demo2_m3"]
MATCH_WINDOW = timedelta(minutes=5)

# (field, label for True, label for False, which flag value the filter would SKIP)
FILTERS = [
    ("shadow_closed_in_favor", "candle agreed with trade", "candle DISAGREED", False),
    ("shadow_low_volume", "LOW volume", "not low volume", True),
    ("shadow_in_excluded_window", "in 08-12 excluded window", "outside window", True),
    ("shadow_ema50_trend_agree", "with EMA50 trend", "AGAINST EMA50 trend", False),
    ("shadow_ema100_trend_agree", "with EMA100 trend", "AGAINST EMA100 trend", False),
]


def read_entries(account: str) -> list[dict]:
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
        if e.get("action") != "trade_entered":
            continue
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        entries.append(e)
    return entries


def match(entries: list[dict], trades: list[dict]) -> list[tuple[dict, dict]]:
    used = set()
    matched = []
    for e in entries:
        best_i, best_score = None, None
        for i, t in enumerate(trades):
            if i in used or t["direction"] != e.get("direction"):
                continue
            t_entry_utc = t["entry_time"].astimezone(timezone.utc)
            if abs((t_entry_utc - e["_ts"]).total_seconds()) > MATCH_WINDOW.total_seconds():
                continue
            price_diff = abs(t["entry_price"] - e.get("entry", 0.0))
            if price_diff > 0.10:
                continue
            if best_score is None or price_diff < best_score:
                best_i, best_score = i, price_diff
        if best_i is not None:
            used.add(best_i)
            matched.append((e, trades[best_i]))
    return matched


def stats(bucket: list[dict]) -> tuple[int, float, float, float]:
    n = len(bucket)
    wins = sum(1 for t in bucket if t["profit"] > 0)
    total = sum(t["profit"] for t in bucket)
    return n, 100 * wins / n if n else 0.0, total, total / n if n else 0.0


def main() -> None:
    for account in ACCOUNTS:
        entries = read_entries(account)
        if not entries:
            print(f"{account}: no trade_entered entries found.\n")
            continue
        config = load_config(account)
        since = min(e["_ts"] for e in entries) - timedelta(minutes=1)
        now = datetime.now(timezone.utc)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
        finally:
            connector.disconnect()
        pairs = match(entries, trades)

        print(f"{'=' * 78}\n{account}: {len(pairs)}/{len(entries)} entries matched to real outcomes\n{'=' * 78}")

        for field, label_true, label_false, skip_value in FILTERS:
            present = [(e, t) for e, t in pairs if field in e]
            if not present:
                print(f"  {field}: not logged on this account yet.")
                continue
            keep_value = not skip_value
            keep_bucket = [t for e, t in present if e[field] == keep_value]
            skip_bucket = [t for e, t in present if e[field] == skip_value]
            keep_label = label_true if keep_value else label_false
            skip_label = label_true if skip_value else label_false

            kn, kwr, ktot, kavg = stats(keep_bucket)
            sn, swr, stot, savg = stats(skip_bucket)
            print(f"  {field}  ({len(present)} trades logged)")
            print(f"     WOULD KEEP ({keep_label:<26}): n={kn:<4} {kwr:5.1f}% win  "
                  f"total ${ktot:+9.2f}  avg ${kavg:+7.2f}/trade")
            print(f"     WOULD SKIP ({skip_label:<26}): n={sn:<4} {swr:5.1f}% win  "
                  f"total ${stot:+9.2f}  avg ${savg:+7.2f}/trade")
            if kn and sn:
                verdict = "HELPS" if savg < kavg else "HURTS"
                print(f"     -> filtering {verdict}: skipped trades avg ${savg:+.2f} vs kept ${kavg:+.2f}; "
                      f"removing them changes total by ${-stot:+.2f}")
            print()


if __name__ == "__main__":
    main()
