"""Where do the losses actually come from?

User's question 2026-09-07: "there are more loss trades, and the losses
amount are big sometimes." That has never been broken down. About a
dozen ENTRY filters have been tested and almost all failed, so this
looks at the losses themselves instead: which exit closed them, which
direction, which hour, and how concentrated the loss dollars are.

BUY vs SELL has never once been split in this project. If one direction
loses far more than the other, that is a large, simple lever that has
been sitting in plain sight.

METHOD, and the trap it is built to avoid: every dimension reports the
NET result and the AVERAGE PER TRADE for that bucket, not just the loss
total. A bucket with many losses usually also has many wins, and ranking
buckets by "total lost" would simply rank them by how many trades they
contain -- the same arithmetic artifact that made the colour+volume
filter look like a winner and then cost $192 live. Only the per-trade
net is a fair comparison between buckets of different sizes.

Uses MT5's own closed trades joined to the engine's logged exit category
by ticket. No candle matching anywhere, so the 2026-09-07 wrong-candle
bug cannot affect any of it.

    python scripts/analyze_loss_anatomy.py --since "2026-08-25 00:00:00"
    python scripts/analyze_loss_anatomy.py --since "2026-08-25 00:00:00" --offset-hours 3

Read-only: reads history and logs. Places no orders.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker UTC offset; supply manually when the market is closed")
    return p.parse_args()


def exit_categories(account: str) -> dict[int, str]:
    """ticket -> the engine's own close category, from decisions.jsonl."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out: dict[int, str] = {}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = e.get("ticket")
            if ticket is None:
                continue
            if e.get("action") == "trade_closed_tp":
                out[int(ticket)] = "take_profit"
            elif e.get("action") == "trade_exited":
                out[int(ticket)] = e.get("category", "unknown")
    return out


def bucket_table(title: str, rows: list[dict], key: str) -> None:
    """Net and per-trade for every bucket. Sorted by per-trade, worst
    first -- ranking by total lost would just rank by bucket size."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[str(r[key])].append(r)
    if not groups:
        return
    print(f"  {title}")
    print(f"    {'bucket':<28}{'n':>5}{'wins':>6}{'win%':>8}{'net $':>12}{'$/trade':>10}{'loss $':>12}")
    for name, group in sorted(groups.items(), key=lambda kv: sum(r["profit"] for r in kv[1]) / len(kv[1])):
        wins = sum(1 for r in group if r["profit"] > 0)
        net = sum(r["profit"] for r in group)
        lost = sum(r["profit"] for r in group if r["profit"] < 0)
        print(f"    {name:<28}{len(group):>5}{wins:>6}{100 * wins / len(group):>7.1f}%"
              f"{net:>+12.2f}{net / len(group):>+10.2f}{lost:>+12.2f}")
    print()


def concentration(rows: list[dict]) -> None:
    """How much of the loss comes from the worst few trades? If a small
    number of trades carries most of the damage, capping THOSE is worth
    more than filtering everything."""
    losses = sorted((r["profit"] for r in rows if r["profit"] < 0))
    if not losses:
        return
    total = sum(losses)
    print(f"  Loss concentration ({len(losses)} losing trades, ${total:.2f} lost)")
    for pct in (10, 20, 50):
        n = max(1, round(len(losses) * pct / 100))
        share = sum(losses[:n]) / total * 100
        print(f"    worst {pct:>2}% ({n:>3} trades) carry {share:>5.1f}% of all loss dollars")
    print(f"    biggest single loss: ${losses[0]:.2f}   median loss: ${losses[len(losses) // 2]:.2f}")
    print()


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    direction_totals: dict[str, list[dict]] = defaultdict(list)

    for account in accounts:
        config = load_config(account)
        categories = exit_categories(account)

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
        finally:
            connector.disconnect()

        rows = []
        for t in trades:
            entry_local = t["entry_time"].astimezone(COLOMBO)
            rows.append({
                "profit": float(t["profit"]),
                "direction": t["direction"],
                "hour": f"{entry_local.hour:02d}:00 Colombo",
                "category": categories.get(int(t["ticket"]), "unmatched"),
                "time": entry_local,
            })
        if not rows:
            print(f"{account}: no trades in this window.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        wins = sum(1 for r in rows if r["profit"] > 0)
        net = sum(r["profit"] for r in rows)
        print("=" * 84)
        print(f"{account} ({config.timeframe}): {len(rows)} trades, {wins} wins "
              f"({100 * wins / len(rows):.1f}%), net ${net:+.2f}")
        print("=" * 84)

        bucket_table("BY DIRECTION", rows, "direction")
        bucket_table("BY EXIT", rows, "category")
        bucket_table("BY ENTRY HOUR", rows, "hour")
        concentration(rows)

        # Direction is the headline unknown -- check it holds in both
        # halves before anyone acts on it.
        mid = len(rows) // 2
        print("  DIRECTION, walk-forward (must hold in BOTH halves to mean anything)")
        for label, half in (("first half ", rows[:mid]), ("second half", rows[mid:])):
            parts = []
            for d in ("BUY", "SELL"):
                sub = [r for r in half if r["direction"] == d]
                if sub:
                    parts.append(f"{d} n={len(sub):<3} ${sum(r['profit'] for r in sub) / len(sub):+7.2f}/trade")
            print(f"    {label}: " + "   ".join(parts))
        print()

        for r in rows:
            direction_totals[r["direction"]].append(r)

    print("=" * 84)
    print("ALL ACCOUNTS COMBINED — BY DIRECTION")
    print("=" * 84)
    print("A direction bias that holds on every account separately is a real effect.")
    print("One that only appears when pooled is usually just the worst account.\n")
    bucket_table("COMBINED", [r for rs in direction_totals.values() for r in rs], "direction")


if __name__ == "__main__":
    main()
