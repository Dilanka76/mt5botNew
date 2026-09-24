"""What if we HELD instead of flipping? The user's own observation, priced.

2026-09-24, the user, watching the live chart: "in the consolidation,
sideway, I can't grab the win because the opposite cross comes and I lose
the trade."

That is NOT the question this project has been testing. Every previous
test either skipped those entries (29 filters, all failed) or stopped the
reversing (the flip-chain test, failed). **None asked what happens if the
trade is simply HELD through the opposite cross and given time to reach
its target.** In a sideways market price comes back -- that is what a
range is -- so the losses he is describing might be wins that were closed
too early.

WHAT IS COMPARED, on real trades, chain by chain:

  a FLIP CHAIN is one fresh entry plus every reversal that followed it
  without the bot going flat (the definition in flip_chain_test.py, which
  this imports rather than copies).

  REALLY      what the whole chain actually earned, every leg of it.
  HELD        what the FIRST trade of that chain would have earned alone
              if the opposite cross had been ignored: closed only by its
              own take-profit, by the broker backstop, or after 48 hours.

That is the fair comparison. Holding means the reversal trades never
happen, so the chain's later legs are not earned either -- both sides of
this table account for that.

SCORED HARSHLY, on purpose: a candle touching both the take-profit and
the backstop counts as the BACKSTOP, because candles cannot say which
came first and the flattering assumption is how backtests lie.

    python scripts/hold_through_cross_test.py --since "2026-09-23 13:50:00"
    python scripts/hold_through_cross_test.py --since "2026-08-25 00:00:00"
    (add --offset-hours 3 when the market is closed)

Read-only. No config, no bot, no order. If holding wins here, the next
step is a year-long test, not a live account.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from flip_chain_test import build_chains          # ONE definition of a chain, not a copy

OZ_PER_LOT = 100.0
MAX_HOLD = timedelta(hours=48)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="live2_m3,live2_m5,demo2_m3,demo2_m5")
    p.add_argument("--since", default="2026-08-25 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    p.add_argument("--detail", action="store_true", help="one line per chain")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def hold_outcome(df, entry_time, entry_price, sign, target, backstop):
    """The first trade of a chain, managed WITHOUT the opposite-cross exit:
    its own take-profit, the broker backstop, or 48 hours. Returns
    (price per ounce moved, why). Only candles that opened at or after the
    entry are used, so nothing here is hindsight."""
    tp = entry_price + sign * target
    stop = entry_price - sign * backstop if backstop else None
    window = df[(df.index >= entry_time) & (df.index <= entry_time + MAX_HOLD)]
    for _, row in window.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        hit_stop = stop is not None and ((lo <= stop) if sign > 0 else (hi >= stop))
        hit_tp = (hi >= tp) if sign > 0 else (lo <= tp)
        if hit_stop:                       # the harsh assumption, deliberately
            return sign * (stop - entry_price), "backstop"
        if hit_tp:
            return sign * (tp - entry_price), "target"
    if len(window):
        last = float(window["close"].iloc[-1])
        return sign * (last - entry_price), "still open at 48h"
    return None, "no candles"


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 96)
    print("HOLD THROUGH THE OPPOSITE CROSS -- the whole flip chain, against holding the first trade")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  A candle touching both target and backstop "
          f"counts as the BACKSTOP.")
    print("=" * 96)

    grand_real = grand_held = 0.0
    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(hours=2), now, offset)
        finally:
            connector.disconnect()

        raw = [t for t in raw if t["entry_time"].astimezone(timezone.utc) >= since]
        if not raw:
            print(f"\n{account}: no trades")
            continue
        rows = build_chains(raw)
        backstop = getattr(config, "broker_backstop_usd", None)
        target = config.take_profit_usd

        chains: dict = {}
        for trade, row in zip(raw, rows):
            c = chains.setdefault(row["chain"], {"legs": [], "first": trade})
            c["legs"].append({"trade": trade, "row": row})

        print(f"\n{'=' * 96}\n{account}   {config.timeframe}   {len(raw)} trades in "
              f"{len(chains)} chains   target ${target:g}   backstop "
              f"{('$' + format(backstop, 'g')) if backstop else 'none'}\n{'=' * 96}")

        real_total = held_total = 0.0
        better = worse = 0
        for chain_id, c in sorted(chains.items()):
            first = c["first"]
            sign = 1.0 if first["direction"] == "BUY" else -1.0
            volume = float(first["volume"])
            entry_utc = first["entry_time"].astimezone(timezone.utc)
            real = sum(float(l["trade"]["profit"]) for l in c["legs"])

            moved, why = hold_outcome(df, entry_utc, float(first["entry_price"]),
                                      sign, target, backstop)
            if moved is None:
                continue
            # the same lot size and the same one-trade commission the first
            # leg really paid, so only the EXIT rule differs
            cost = real - sum(
                (1.0 if l["trade"]["direction"] == "BUY" else -1.0)
                * (float(l["trade"]["exit_price"]) - float(l["trade"]["entry_price"]))
                * float(l["trade"]["volume"]) * OZ_PER_LOT for l in c["legs"])
            one_leg_cost = cost / max(len(c["legs"]), 1)
            held = moved * volume * OZ_PER_LOT + one_leg_cost

            real_total += real
            held_total += held
            better += held > real
            worse += held < real
            if args.detail:
                print(f"  {first['entry_time']:%d %b %H:%M} {first['direction']:<4} "
                      f"{len(c['legs'])} leg(s)  really {money(real):>9}   "
                      f"held {money(held):>9}  ({why})")

        print(f"\n  really (all legs)   {money(real_total)}")
        print(f"  held the first      {money(held_total)}   -> {money(held_total - real_total)}")
        print(f"  holding was better on {better} chains, worse on {worse}")
        grand_real += real_total
        grand_held += held_total

    print(f"\n{'=' * 96}")
    print(f"ALL ACCOUNTS   really {money(grand_real)}   held {money(grand_held)}   "
          f"-> {money(grand_held - grand_real)}")
    print("\nWhat this canNOT tell you: candles hide the order of moves inside one candle (scored")
    print("as the backstop here), and a held trade ties up the account, so entries the bot really")
    print("took while flat would not have happened. A promising number here means ONE thing: run")
    print("it over a year next, not on a live account.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
