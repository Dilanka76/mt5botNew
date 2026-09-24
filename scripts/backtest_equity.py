"""The ride, not the total: drawdown, streaks and months from a backtest.

A backtest report says what a year EARNED. It does not say what the year
FELT like -- how deep the holes got, how long the losing runs ran, how
many months were red. Those are the numbers that decide whether a $300
account is still alive at the end, and they are the reason this exists
(2026-09-24: live2 went from $175 to $5.83 in one night while the
strategy's month-long total was still positive).

Reads the trades.jsonl a backtest run already wrote, so it costs nothing
and cannot disagree with the report it came from.

WHAT IT PRINTS
  the ride        worst peak-to-trough fall in money and in %, the
                  longest losing run, the biggest single loss
  month by month  every month's net, so a good stretch cannot hide a
                  year of bleeding
  costs           the backtest does NOT charge commission; this adds it
                  back at --commission-per-lot so the number is honest

    python scripts/backtest_equity.py reports\\backtest\\demo2_m5\\2025-09-01_2026-09-01.trades.jsonl
    python scripts/backtest_equity.py <file> --start-balance 300 --commission-per-lot 6

Read-only. Touches no bot, no MT5, no config.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trades", help="the .trades.jsonl a backtest wrote")
    p.add_argument("--start-balance", type=float, default=300.0)
    p.add_argument("--commission-per-lot", type=float, default=6.0,
                   help="charged per lot at entry; the backtest itself charges nothing")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def main() -> None:
    args = parse_args()
    path = Path(args.trades)
    if not path.is_file():
        raise SystemExit(f"not found: {path}")

    trades = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        cost = float(t.get("volume", 0.0)) * args.commission_per_lot
        trades.append({"when": datetime.fromisoformat(t["close_time"]),
                       "raw": float(t["profit"]),
                       "net": float(t["profit"]) - cost,
                       "cost": cost,
                       "volume": float(t.get("volume", 0.0))})
    if not trades:
        raise SystemExit("no trades in that file")
    trades.sort(key=lambda t: t["when"])

    for label, key in (("as the backtest reports it (no commission)", "raw"),
                       (f"with commission at ${args.commission_per_lot:g} a lot", "net")):
        balance = args.start_balance
        peak = balance
        worst, worst_pct, low = 0.0, 0.0, balance
        streak = best_streak = 0
        wins = 0
        for t in trades:
            balance += t[key]
            peak = max(peak, balance)
            low = min(low, balance)
            if peak - balance > worst:
                worst, worst_pct = peak - balance, 100 * (peak - balance) / peak if peak else 0.0
            if t[key] > 0:
                wins += 1
                streak = 0
            else:
                streak += 1
                best_streak = max(best_streak, streak)

        print("=" * 84)
        print(f"{label.upper()}")
        print("=" * 84)
        print(f"  trades              {len(trades)}   won {wins} ({100 * wins / len(trades):.1f}%)")
        print(f"  net                 {money(sum(t[key] for t in trades))}")
        print(f"  from {money(args.start_balance).lstrip('+')} it ends at   {money(balance).lstrip('+')}"
              f"   (lowest it went: {money(low).lstrip('+')})")
        print(f"  worst fall          -${worst:,.2f} ({worst_pct:.0f}% of the peak)")
        print(f"  longest losing run  {best_streak} trades in a row")
        print(f"  biggest single loss {money(min(t[key] for t in trades))}")
        if key == "raw":
            print(f"  commission it ignores {money(-sum(t['cost'] for t in trades))} "
                  f"over {sum(t['volume'] for t in trades):.2f} lots")
        print()

    months: OrderedDict = OrderedDict()
    for t in trades:
        m = months.setdefault(t["when"].strftime("%Y-%m"), {"n": 0, "net": 0.0})
        m["n"] += 1
        m["net"] += t["net"]
    red = sum(1 for m in months.values() if m["net"] < 0)
    print("=" * 84)
    print(f"MONTH BY MONTH, after commission -- {red} of {len(months)} months lost money")
    print("=" * 84)
    for name, m in months.items():
        bar = "#" * min(40, int(abs(m["net"]) / 10))
        print(f"  {name}   {m['n']:>4} trades  {money(m['net']):>11}  {bar}")

    print("\n" + "=" * 84)
    print("READ THE WORST FALL FIRST. A year's total says whether the edge exists; the fall")
    print("says whether an account survives long enough to collect it. The backtest's own")
    print("report lists its approximations (synthetic ticks, synthesized spread) -- they are")
    print("real, and they are the reason this is a shape to judge, not a number to trust.")


if __name__ == "__main__":
    main()
