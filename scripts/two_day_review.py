"""How the accounts actually traded, day by day, with the broken window marked.

User, 2026-09-15: *"tell me how our trades are working both two days,
yesterday and today, after the weekend, both demo accounts"*.

WHY NOT daily_report.py OR compare_today.py. Both average a whole day. On
2026-09-14 that is not a number, it is two different systems bolted
together: until ~06:17 UTC close_position() raised NameError on every call,
so no software exit worked anywhere, and one orphaned trade froze each M3
account for 3h45m while eight crosses were refused. Averaging that with the
afternoon hides the fault AND understates the fixed build.

So this splits every day at --fix-utc and reports the halves separately,
with the number that matters for the broken window -- how many entries were
REFUSED -- taken from decisions.jsonl rather than from trade history, since
a refused entry leaves no trade behind to count.

Exit reasons come from decisions.jsonl too. MT5 history knows a position
closed; only the bot knows whether that was a breakeven, a stop, a swap or
a take-profit, and that breakdown is the whole story of whether the exits
are working.

    python scripts/two_day_review.py
    python scripts/two_day_review.py --days 3

Read-only: reads MT5 history and log files. Places nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import StaleTickError, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")

# 2026-09-14 06:17 UTC: every bot restarted on the build where
# close_position() works and a forgotten position is re-adopted.
DEFAULT_FIX = "2026-09-14T06:17"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m3,demo1_m5,demo2_m3,demo2_m5")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--fix-utc", default=DEFAULT_FIX,
                   help="ISO time the fixed build went live; days are split here")
    return p.parse_args()


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def read_decisions(account: str, since: datetime) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue          # half-written line from a crash mid-append
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            e["_ts"] = ts
            out.append(e)
    return out


def summarise(label: str, trades: list[dict], decisions: list[dict], indent: str = "    ") -> None:
    if not trades and not decisions:
        print(f"{indent}{label}: nothing")
        return
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] < 0]
    total = sum(t["profit"] for t in trades)
    rate = (len(wins) / len(trades) * 100) if trades else 0.0
    blocked = [d for d in decisions if d.get("action") == "entry_blocked_existing_position"]

    print(f"{indent}{label}")
    print(f"{indent}  {len(trades)} trade(s)   {len(wins)}W / {len(losses)}L"
          f"   win rate {rate:.0f}%   net {money(total)}")
    if wins:
        print(f"{indent}  average win  {money(sum(t['profit'] for t in wins) / len(wins))}")
    if losses:
        print(f"{indent}  average loss {money(sum(t['profit'] for t in losses) / len(losses))}")

    exits: dict[str, int] = defaultdict(int)
    for d in decisions:
        if d.get("action") == "trade_exited":
            exits[d.get("category", "?")] += 1
        elif d.get("action") == "trade_closed_tp":
            exits["take_profit (broker)"] += 1
    if exits:
        parts = ", ".join(f"{k} x{v}" for k, v in sorted(exits.items(), key=lambda kv: -kv[1]))
        print(f"{indent}  exits: {parts}")
    else:
        print(f"{indent}  exits: NONE LOGGED BY THE BOT "
              f"— every close came from the broker, not the strategy")

    if blocked:
        print(f"{indent}  *** {len(blocked)} ENTRIES REFUSED "
              f"(engine flat, broker held a position — a trade was orphaned) ***")


def main() -> None:
    args = parse_args()
    fix = datetime.fromisoformat(args.fix_utc).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)

    print("=" * 84)
    print(f"TWO-DAY REVIEW — {since:%Y-%m-%d} to {now:%Y-%m-%d} (Colombo days)")
    print(f"fixed build live from {fix:%Y-%m-%d %H:%M} UTC; each day is split there")
    print("=" * 84)

    grand: dict[str, float] = defaultdict(float)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        if not (PROJECT_ROOT / f".env.{account}").exists():
            print(f"\n{account}: no credentials, skipped.")
            continue
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            info = connector.account_info()
            offset = mt5_utc_offset(connector, config.symbol)
            deals = mt5.history_deals_get(since + offset, now + offset) or []
        finally:
            connector.disconnect()

        by_position: dict[int, list] = defaultdict(list)
        for d in deals:
            if d.symbol == config.symbol and d.magic == config.execution.magic_number:
                by_position[d.position_id].append(d)

        trades = []
        for pid, ds in by_position.items():
            ds.sort(key=lambda d: d.time)
            entry = next((d for d in ds if d.entry == 0), None)
            if entry is None or not any(d.entry == 1 for d in ds):
                continue          # never opened here, or still open
            closed = max(d.time for d in ds if d.entry == 1)
            trades.append({
                "closed": datetime.fromtimestamp(closed, tz=timezone.utc) - offset,
                "profit": sum(d.profit + d.commission + d.swap for d in ds),
                "dir": "BUY" if entry.type == 0 else "SELL",
            })

        decisions = read_decisions(account, since)

        print(f"\n{'=' * 84}")
        print(f"{account}   {config.symbol}   balance {money(info.balance)}")
        print("=" * 84)

        days = sorted({t["closed"].astimezone(COLOMBO).date() for t in trades}
                      | {d["_ts"].astimezone(COLOMBO).date() for d in decisions})
        for day in days:
            day_trades = [t for t in trades if t["closed"].astimezone(COLOMBO).date() == day]
            day_dec = [d for d in decisions if d["_ts"].astimezone(COLOMBO).date() == day]
            print(f"\n  {day}   ({len(day_trades)} trade(s), net "
                  f"{money(sum(t['profit'] for t in day_trades))})")
            before_t = [t for t in day_trades if t["closed"] < fix]
            after_t = [t for t in day_trades if t["closed"] >= fix]
            before_d = [d for d in day_dec if d["_ts"] < fix]
            after_d = [d for d in day_dec if d["_ts"] >= fix]
            if before_t or before_d:
                summarise("BEFORE the fix  (broken: no software exits)", before_t, before_d)
            if after_t or after_d:
                summarise("AFTER the fix", after_t, after_d)
            grand[account] += sum(t["profit"] for t in after_t)

    print(f"\n{'=' * 84}")
    print("NET PER ACCOUNT, FIXED BUILD ONLY (the only comparable period)")
    for account, total in grand.items():
        print(f"  {account:<12} {money(total)}")
    print(f"  {'TOTAL':<12} {money(sum(grand.values()))}")
    print("\nThe pre-fix window is shown for completeness, NOT for comparison:")
    print("software exits could not run at all, so those trades measure the bug.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED — the broker clock offset cannot be measured.")
        print(f"  {exc}")
        raise SystemExit(1)
