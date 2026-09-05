"""Why do MT5 trade times and decisions.jsonl timestamps disagree?

Found 2026-09-05: five demo1_m3 trades from 09-04 appear in
scripts/show_losses_today.py at entry prices identical to
decisions.jsonl entries, but roughly 10h19m apart in time. That is not a
timezone, not the broker's known +3h offset, and not DST. This project
has already found FOUR separate MT5-vs-UTC time bugs (see
project_dual_cross_and_cross_confirmed), so a fifth is plausible -- but
it could equally be that those are different trades that happened to
fill at the same price.

This settles it by matching on TICKET rather than price, and by showing
every conversion step from the raw MT5 epoch onward:

  raw deal epoch  ->  naive UTC reading  ->  minus measured offset  ->
  what get_closed_trades_range reports

then puts that next to the decisions.jsonl line carrying the same
ticket. decisions.jsonl timestamps come from datetime.now(timezone.utc)
-- plain system UTC, essentially impossible to get wrong -- so they are
the reference.

Matching uses the EXIT: trade_exited / trade_closed_tp both carry a
"ticket" field, while trade_entered does not. The entry decision is then
the trade_entered immediately preceding that exit.

    python scripts/diagnose_trade_time_mismatch.py --account demo1_m3 --date 2026-09-04

Read-only: reads MT5 deal history and decisions.jsonl, changes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo1_m3")
    p.add_argument("--date", required=True, help="YYYY-MM-DD (Colombo calendar day)")
    p.add_argument("--limit", type=int, default=8, help="max trades to print")
    return p.parse_args()


def read_decisions(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        out.append(e)
    return out


def main() -> None:
    args = parse_args()
    account = validate_account_name(args.account)
    config = load_config(account)
    day = datetime.strptime(args.date, "%Y-%m-%d").date()

    decisions = read_decisions(account)
    by_ticket: dict[int, dict] = {}
    for e in decisions:
        if e.get("action") in ("trade_exited", "trade_closed_tp") and e.get("ticket") is not None:
            by_ticket[int(e["ticket"])] = e

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = mt5_utc_offset(connector, config.symbol)
        # Query generously in BROKER time, then inspect the raw epochs.
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=1)
        end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc) + timedelta(days=1)
        deals = mt5.history_deals_get(start + offset, end + offset)
    finally:
        connector.disconnect()

    print(f"{'=' * 78}\n{account}  --  {args.date}")
    print(f"measured mt5_utc_offset = {offset} ({offset.total_seconds()/3600:+.2f}h)")
    print(f"system UTC now          = {datetime.now(timezone.utc).isoformat()}")
    print(f"{'=' * 78}\n")

    if not deals:
        print("No deals returned for this window.")
        return

    relevant = [d for d in deals
                if d.symbol == config.symbol and d.magic == config.execution.magic_number]
    by_position: dict[int, list] = {}
    for d in relevant:
        by_position.setdefault(d.position_id, []).append(d)

    shown = 0
    for position_id, deal_list in sorted(by_position.items()):
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_OUT), None)
        if entry_deal is None or exit_deal is None:
            continue

        # Reproduce get_closed_trades_range's conversion, step by step.
        raw_entry = entry_deal.time
        naive_utc = datetime.fromtimestamp(raw_entry, tz=timezone.utc)
        corrected = naive_utc - offset
        as_colombo = corrected.astimezone(COLOMBO)

        decision = by_ticket.get(int(position_id))

        print(f"ticket {position_id}   entry price {entry_deal.price:.2f}")
        print(f"   raw MT5 epoch                : {raw_entry}")
        print(f"   read as UTC (no correction)  : {naive_utc.isoformat()}")
        print(f"   minus offset ({offset.total_seconds()/3600:+.0f}h)        : {corrected.isoformat()}")
        print(f"   -> reported as Colombo       : {as_colombo.strftime('%Y-%m-%d %H:%M:%S')}")
        if decision:
            d_ts = decision["_ts"]
            raw_exit_naive = datetime.fromtimestamp(exit_deal.time, tz=timezone.utc)
            reported_exit = raw_exit_naive - offset
            gap = (reported_exit - d_ts).total_seconds() / 3600
            print(f"   decisions.jsonl EXIT (true UTC): {d_ts.isoformat()}   [{decision.get('action')}]")
            print(f"   MT5 exit after correction     : {reported_exit.isoformat()}")
            print(f"   *** GAP = {gap:+.2f} hours ***" if abs(gap) > 0.05 else "   gap: none (times agree)")
        else:
            print(f"   decisions.jsonl: NO entry found with ticket {position_id}")
        print()

        shown += 1
        if shown >= args.limit:
            break

    print(f"{'=' * 78}")
    print("If the GAP is consistently non-zero, get_closed_trades_range's conversion is")
    print("wrong and every per-trade TIME in this project's reports is affected (P/L and")
    print("prices are not). If the gap is zero, the earlier price-based matching was")
    print("simply pairing up different trades that filled at the same price.")


if __name__ == "__main__":
    main()
