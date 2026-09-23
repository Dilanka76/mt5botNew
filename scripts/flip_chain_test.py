"""The FLIP CHAIN -- what a sideways market really costs this bot.

User, 2026-09-23: "the main focus should be catch chop, consolidate,
sideway." Eight different ways of judging chop AT THE ENTRY have now been
tested and failed (ADX, EMA50/100, Efficiency Ratio, the M15 box, the H1
trap, the Asian box, double confirmation, and 'how straight were the last
4 hours' -- which said choppy entries earn the MOST). Only the
trader-drawn two-touch range is still open, and that test is already
running live on demo2.

So this script asks the question from the other side, which has never
been asked here: not "should we ENTER in chop?" but "should we keep
FLIPPING in chop?"

WHY THAT IS A DIFFERENT QUESTION. The bot's exit is a reversal: on the
opposite cross it closes and immediately opens the other way. In a
sideways market that fires again and again. live2_m3, 2026-09-22:

    05:21 SELL 0.06  -$57.06   ]
    05:33 BUY  0.06  -$53.34   ]  three flips in 36 minutes
    05:57 SELL 0.04  +$23.88   ]

A FLIP CHAIN is what that is: a fresh entry, then every reversal that
follows it without the bot ever going flat. Depth 0 is the fresh entry,
depth 1 the first flip, depth 2 the second, and so on.

WHAT IS MEASURED, all from real closed trades:

  by depth      each flip depth on its own: count, win rate, $/oz, halves
  by chain      the WHOLE chain's net -- because a chain that loses twice
                and then wins big is not a chain we want to cut
  the rule      "stop reversing once the chain reaches depth k": every
                trade at or past depth k disappears, priced in real money

THE BAR, set before the first run: stopping at depth k is worth building
only if it GAINS money in BOTH halves, on BOTH M3 accounts AND both M5
accounts, and the group it removes holds at least 20 trades. "Earns
less" is not "loses" -- that distinction has saved real money here
repeatedly.

HONEST LIMIT, printed with the result: a chain cut short leaves the bot
FLAT, so it would have entered fresh on some later cross. That
alternative history is not simulated, so a rule's real value is not
exactly the number printed -- the number is the trades removed, nothing
more.

    python scripts/flip_chain_test.py --since "2026-08-25 00:00:00"
    python scripts/flip_chain_test.py --offset-hours 3      (weekends)

Read-only. Touches no bot, changes no config.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0
SWAP_EXIT = "EMA Cross Exit"     # bot/analytics.classify_exit_reason
LINK_SECONDS = 120               # a reversal re-entry lands within seconds of its own close
MIN_GROUP = 20


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5,live2_m3,live2_m5,demo1_m3,demo1_m5")
    p.add_argument("--since", default="2026-08-25 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def show(label: str, rows: list, order: dict, half: int) -> None:
    if not rows:
        print(f"    {label:<24} none")
        return
    w = sum(1 for r in rows if r["oz"] > 0)
    first = [r["oz"] for r in rows if order[id(r)] < half]
    second = [r["oz"] for r in rows if order[id(r)] >= half]
    flag = ""
    if len(rows) >= MIN_GROUP and mean(first) < 0 and mean(second) < 0:
        flag = "   <- loses in both halves"
    print(f"    {label:<24} {len(rows):>4} trades {100 * w / len(rows):>4.0f}% won "
          f"{mean([r['oz'] for r in rows]):>+6.2f} $/oz   halves {mean(first):>+6.2f} /"
          f" {mean(second):>+6.2f}   {money(sum(r['profit'] for r in rows)):>10}{flag}")


def build_chains(trades: list) -> list:
    """Walks the real trades in time order and stamps each one with its
    flip depth. A trade continues the previous chain only when the
    previous trade was closed BY THE OPPOSITE CROSS, this one runs the
    other way, and it opened within seconds of that close -- which is
    exactly what the engine's reversal does. A take-profit ends a chain
    even if the next entry follows quickly, because the bot really was
    flat in between."""
    rows, depth, chain_id = [], 0, 0
    previous = None
    for t in trades:
        linked = (previous is not None
                  and previous["exit_reason"] == SWAP_EXIT
                  and t["direction"] != previous["direction"]
                  and 0 <= (t["entry_time"] - previous["exit_time"]).total_seconds() <= LINK_SECONDS)
        if linked:
            depth += 1
        else:
            depth = 0
            chain_id += 1
        volume = float(t["volume"])
        rows.append({
            "entry": t["entry_time"],
            "profit": float(t["profit"]),
            "oz": float(t["profit"]) / (volume * OZ_PER_LOT),
            "depth": depth,
            "chain": chain_id,
        })
        previous = t
    return rows


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("THE FLIP CHAIN -- how the bot behaves once a sideways market starts flipping it")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  Outcome in $/oz, so lot size tilts nothing.")
    print("=" * 100)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
        finally:
            connector.disconnect()

        raw = [t for t in raw if t["entry_time"].astimezone(timezone.utc) >= since]
        rows = build_chains(raw)
        if not rows:
            print(f"\n{account}: no trades")
            continue
        order = {id(r): i for i, r in enumerate(rows)}      # already in entry order
        half = len(rows) // 2
        real = sum(r["profit"] for r in rows)

        print(f"\n{'=' * 100}\n{account}   {config.timeframe}   {len(rows)} trades   "
              f"really {money(real)}\n{'=' * 100}")

        print("  each flip depth on its own")
        show("depth 0  fresh entry", [r for r in rows if r["depth"] == 0], order, half)
        show("depth 1  first flip", [r for r in rows if r["depth"] == 1], order, half)
        show("depth 2  second flip", [r for r in rows if r["depth"] == 2], order, half)
        show("depth 3+ deep chop", [r for r in rows if r["depth"] >= 3], order, half)

        print("\n  the WHOLE chain, start to finish")
        chains: dict = {}
        for r in rows:
            c = chains.setdefault(r["chain"], {"n": 0, "profit": 0.0, "oz": 0.0})
            c["n"] += 1
            c["profit"] += r["profit"]
            c["oz"] += r["oz"]
        for label, keep in (("no flip (1 trade)", lambda n: n == 1),
                            ("2 trades", lambda n: n == 2),
                            ("3 trades", lambda n: n == 3),
                            ("4 or more", lambda n: n >= 4)):
            sel = [c for c in chains.values() if keep(c["n"])]
            if not sel:
                print(f"    {label:<24} none")
                continue
            good = sum(1 for c in sel if c["profit"] > 0)
            print(f"    {label:<24} {len(sel):>4} chains {100 * good / len(sel):>4.0f}% ended up "
                  f"{mean([c['oz'] for c in sel]):>+6.2f} $/oz   "
                  f"{money(sum(c['profit'] for c in sel)):>10}")

        print("\n  THE RULE: stop reversing once the chain reaches this depth")
        print(f"    {'stop at':>9} {'trades cut':>11} {'net':>12} {'vs real':>11} "
              f"{'1st half':>10} {'2nd half':>10}")
        for k in (1, 2, 3):
            cut = [r for r in rows if r["depth"] >= k]
            if not cut:
                continue
            d1 = -sum(r["profit"] for r in cut if order[id(r)] < half)
            d2 = -sum(r["profit"] for r in cut if order[id(r)] >= half)
            net = real - sum(r["profit"] for r in cut)
            print(f"    {'depth ' + str(k):>9} {len(cut):>11} {money(net):>12} "
                  f"{money(net - real):>11} {money(d1):>10} {money(d2):>10}")

    print(f"\n{'=' * 100}")
    print("THE BAR (set before this run): stopping at a depth is worth building only if it GAINS")
    print("in BOTH halves, on BOTH M3 accounts AND both M5 accounts, cutting at least "
          f"{MIN_GROUP} trades.")
    print("A cut chain leaves the bot FLAT, so it would have entered fresh on some later cross --")
    print("that alternative history is NOT simulated here. And a chain that loses twice then wins")
    print("big is a chain we keep: read the whole-chain block before the per-depth one.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
