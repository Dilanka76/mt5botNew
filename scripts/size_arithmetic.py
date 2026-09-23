"""The same real trades, at different position sizes. Arithmetic, not a strategy.

User, 2026-09-23, after 27 entry and exit ideas had failed: the signal is
not the problem. A normal bad week took 74% of the real account while the
same strategy on demo was barely scratched. The only difference was how
big we were relative to the money.

So this replays the REAL trades -- every one that actually happened, in
the order it happened, with its real outcome -- and changes nothing but
the lot size. It answers one question: where would $300 stand today?

WHY THE OUTCOME PER OUNCE DOES NOT CHANGE WITH LOT SIZE. Every target,
stop and backstop in this bot is a PRICE distance ($/oz), decided before
the order and untouched by volume, so a trade closes at the same price
whatever size it was. Commission is charged per lot, so it scales
exactly too. That makes $/oz the one honest unit here -- and it is why
this is arithmetic rather than a simulation. What is NOT modelled:
slippage at larger size (tiny at these volumes, real at 10x), and
margin -- a real account can be stopped out by the broker before its
balance reaches zero.

WHAT IS COMPARED (no threshold hunting -- these are fixed sizing rules,
not a sweep looking for a winner):

  the ladder we run now      each leg's real position_sizing tiers
  half / a third / a quarter the same ladder, scaled down
  always the minimum         0.01 lots, every trade
  smooth, same size today    no steps: lots grow with every dollar,
                             set to match today's ladder size at $300

Both legs of an account share one balance, exactly as they really do.
Each trade's size is decided from the balance at the moment it OPENED,
and the profit lands when it CLOSED -- the same order the bot itself
sees.

    python scripts/size_arithmetic.py --accounts demo2_m3,demo2_m5 --since "2026-08-25 00:00:00"
    python scripts/size_arithmetic.py --accounts live2_m3,live2_m5 --since "2026-09-16 00:00:00"
    (add --offset-hours 3 when the market is closed)

Read-only. Changes no config and touches no bot. Position size stays the
user's decision -- this only shows what each choice would have done.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots

OZ_PER_LOT = 100.0
MIN_LOT = 0.01
REFERENCE_BALANCE = 300.0      # the balance the smooth rule is matched to


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5",
                   help="the legs sharing ONE balance, e.g. live2_m3,live2_m5")
    p.add_argument("--since", default="2026-08-25 00:00:00", help="true UTC")
    p.add_argument("--start-balance", type=float, default=300.0)
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def down_to_step(lots: float) -> float:
    """Brokers take whole 0.01 steps, and rounding DOWN is the honest
    direction when the question is 'what if we were smaller'."""
    return max(MIN_LOT, int(lots * 100 + 1e-9) / 100)


def ladder_rule(scale: float):
    def rule(balance: float, tiers: list, _smooth: float) -> float:
        return down_to_step(calculate_lots(balance, tiers) * scale)
    return rule


def flat_minimum(_balance: float, _tiers: list, _smooth: float) -> float:
    return MIN_LOT


def smooth_rule(balance: float, _tiers: list, dollars_per_step: float) -> float:
    """No steps: one 0.01 more for every fixed slice of balance, set so
    that at $300 it hands out exactly what today's ladder hands out."""
    return down_to_step(balance / dollars_per_step * MIN_LOT)


RULES = [
    ("the ladder we run now", ladder_rule(1.0)),
    ("half the ladder", ladder_rule(0.5)),
    ("a third of the ladder", ladder_rule(1 / 3)),
    ("a quarter of the ladder", ladder_rule(0.25)),
    ("always the minimum 0.01", flat_minimum),
    ("smooth, same size today", smooth_rule),
]


def run(rule, trades: list, tiers: dict, smooth: dict, start: float) -> dict:
    """Walks the real trades in real time order. Size is decided when a
    trade OPENS, from the balance right then; the money lands when it
    CLOSES. Exits are applied before entries at the same timestamp,
    because a reversal closes first and the bot reads the balance after."""
    events = []
    for i, t in enumerate(trades):
        events.append((t["entry_time"], 1, i))      # 1 = entry, sorted after exits
        events.append((t["exit_time"], 0, i))
    events.sort(key=lambda e: (e[0], e[1]))

    balance, peak, low = start, start, start
    worst_dd, worst_dd_pct = 0.0, 0.0
    lots: dict = {}
    taken, skipped = 0, 0
    wiped_at = None

    for when, kind, i in events:
        t = trades[i]
        if kind == 1:
            if wiped_at is not None:
                continue
            size = rule(balance, tiers[t["account"]], smooth[t["account"]])
            lots[i] = size
            taken += 1
        else:
            if i not in lots:
                skipped += 1
                continue
            balance += t["oz"] * lots[i] * OZ_PER_LOT
            peak = max(peak, balance)
            low = min(low, balance)
            if peak - balance > worst_dd:
                worst_dd = peak - balance
                worst_dd_pct = 100 * worst_dd / peak if peak else 0.0
            if balance <= 0 and wiped_at is None:
                wiped_at = when
    return {"balance": balance, "low": low, "worst_dd": worst_dd,
            "worst_dd_pct": worst_dd_pct, "taken": taken, "skipped": skipped,
            "wiped_at": wiped_at}


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a.strip()) for a in args.accounts.split(",")]

    trades: list = []
    tiers: dict = {}
    smooth: dict = {}
    for account in accounts:
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
        tiers[account] = config.position_sizing
        today_lots = calculate_lots(REFERENCE_BALANCE, config.position_sizing)
        smooth[account] = REFERENCE_BALANCE / (today_lots / MIN_LOT)
        for t in raw:
            if t["entry_time"].astimezone(timezone.utc) < since:
                continue
            volume = float(t["volume"])
            if volume <= 0:
                continue
            trades.append({"account": account,
                           "entry_time": t["entry_time"], "exit_time": t["exit_time"],
                           "oz": float(t["profit"]) / (volume * OZ_PER_LOT)})

    if not trades:
        print("no trades in that window")
        return
    trades.sort(key=lambda t: t["entry_time"])

    print("=" * 96)
    print("THE SAME REAL TRADES, AT DIFFERENT SIZES -- arithmetic on what already happened")
    print(f"{', '.join(accounts)}   {len(trades)} trades   "
          f"{trades[0]['entry_time']:%d %b} to {trades[-1]['entry_time']:%d %b}   "
          f"starting balance {money(args.start_balance)}")
    print("=" * 96)
    for account in accounts:
        rungs = ", ".join(
            f"{'above' if t.max_balance is None else 'to $' + format(t.max_balance, '.0f')}"
            f" -> {t.lots:g}" for t in tiers[account])
        print(f"  {account:<10} ladder now: {rungs}")
        print(f"  {'':<10} smooth rule: 0.01 more lots per "
              f"{money(smooth[account])} of balance")

    print(f"\n  {'sizing rule':<26} {'ends at':>12} {'lowest':>11} "
          f"{'worst drop':>12} {'trades':>8}")
    for label, rule in RULES:
        r = run(rule, trades, tiers, smooth, args.start_balance)
        end = "WIPED OUT" if r["wiped_at"] else money(r["balance"])
        note = f"   wiped out {r['wiped_at']:%d %b %H:%M}" if r["wiped_at"] else ""
        print(f"  {label:<26} {end:>12} {money(r['low']):>11} "
              f"{money(r['worst_dd']):>9} {r['worst_dd_pct']:>3.0f}% {r['taken']:>8}{note}")

    print(f"\n{'=' * 96}")
    print("HOW TO READ THIS. Every rule takes exactly the same trades -- none is a better")
    print("strategy, and the one that ends highest is simply the one that was biggest while")
    print("this particular month happened. Read 'worst drop' first: that is the size of hole")
    print("each rule digs in a bad run, and the next bad run can always be deeper than the")
    print("one in this data. Not modelled: slippage at larger size, and margin -- a real")
    print("account can be stopped out by the broker before its balance ever reaches zero.")
    print("Position size is the account owner's decision; this script only prices it.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
