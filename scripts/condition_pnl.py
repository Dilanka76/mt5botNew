"""Which MARKET CONDITION does the money come from, and which one eats it?

User, 2026-09-23: "the loss happens most of the time in the consolidation,
the range area -- the bot needs to look at the market condition every time
and get a decision: the trend, buy or sell, or consolidation."

The bot already judges ONE condition at entry: the trader-drawn M15 range
(bot/indicators/range_filter.py, live on demo2). This measures the other
two things scripts/market_now.py shows, against every real trade, so a
decision about them can rest on money rather than on the idea being
appealing:

  RANGE       is price inside an M15 ceiling+floor, each touched twice
  STRAIGHT    of the last 4 hours of M15 movement, how much was in one
              direction: |net| / total. 1.0 = a clean trend, 0 = chop
  CHURN       how many M3 EMA13/21 crosses fired in the 2 hours before
              the entry -- the bot flips on each one, so this is the
              price a range charges in trades

Everything is read from candles that had CLOSED before the entry, so no
result here could be known only afterwards.

THE BAR, written before the first run: a condition is worth building into
the bot only if its bad group LOSES money ($/oz below zero) in BOTH halves
of the period, on BOTH M3 accounts AND both M5 accounts, with at least 20
trades in the group. Twenty-odd entry ideas have already failed here; the
bar is what stops the twenty-first from reaching live money on a story.

    python scripts/condition_pnl.py --since "2026-08-25 00:00:00"
    python scripts/condition_pnl.py --since "2026-09-09 00:00:00" --offset-hours 3

Read-only.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.indicators.range_filter import in_range, range_levels, with_atr
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0
LOOKBACK = 16          # M15 candles behind the range check (4 hours)
STRAIGHT_HOURS = 4
CHURN_HOURS = 2
MIN_GROUP = 20


class _On:
    enabled = True


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
        print(f"    {label:<26} none")
        return
    w = sum(1 for r in rows if r["oz"] > 0)
    first = [r["oz"] for r in rows if order[id(r)] < half]
    second = [r["oz"] for r in rows if order[id(r)] >= half]
    flag = ""
    if len(rows) >= MIN_GROUP and mean(first) < 0 and mean(second) < 0:
        flag = "   <- loses in both halves"
    print(f"    {label:<26} {len(rows):>4} trades {100 * w / len(rows):>4.0f}% won "
          f"{mean([r['oz'] for r in rows]):>+6.2f} $/oz   halves {mean(first):>+6.2f} /"
          f" {mean(second):>+6.2f}   {money(sum(r['profit'] for r in rows)):>10}{flag}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 104)
    print("MARKET CONDITION vs MONEY -- every real trade, conditions read from CLOSED candles only")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  Outcome in $/oz, so lot size tilts nothing.")
    print("=" * 104)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            m15 = get_ohlc_range(connector, config.symbol, "M15",
                                 since - timedelta(days=3), now, offset)
            m3 = get_ohlc_range(connector, config.symbol, "M3",
                                since - timedelta(days=3), now, offset)
        finally:
            connector.disconnect()
        m15 = with_atr(m15)
        m3 = compute_emas(m3, config.ema_periods)
        m3_side = (m3["ema13"] > m3["ema21"])

        rows = []
        for t in raw:
            entry = t["entry_time"].astimezone(timezone.utc)
            if entry < since:
                continue
            closed15 = m15[m15.index + timedelta(minutes=15) <= entry]
            if len(closed15) < LOOKBACK:
                continue
            window = closed15.tail(LOOKBACK)
            ceiling, floor = range_levels(window)
            price = float(t["entry_price"])
            boxed = in_range(price, 1.0 if ceiling is not None else 0.0,
                             ceiling if ceiling is not None else float("nan"),
                             floor if floor is not None else float("nan"), _On())

            straight_window = closed15.tail(int(STRAIGHT_HOURS * 60 / 15))
            net = float(straight_window["close"].iloc[-1] - straight_window["open"].iloc[0])
            total = float((straight_window["close"] - straight_window["open"]).abs().sum())
            straight = abs(net) / total if total else 0.0

            side = m3_side[m3_side.index + timedelta(minutes=3) <= entry].tail(
                int(CHURN_HOURS * 60 / 3))
            churn = int((side != side.shift(1)).iloc[1:].sum()) if len(side) > 1 else 0

            rows.append({"entry": entry, "profit": float(t["profit"]),
                         "oz": float(t["profit"]) / (float(t["volume"]) * OZ_PER_LOT),
                         "boxed": bool(boxed), "straight": straight, "churn": churn})

        print(f"\n{account}   {config.timeframe}   {len(rows)} trades")
        if len(rows) < 2 * MIN_GROUP:
            print("    too few trades here to split; shown for completeness")
        if not rows:
            continue
        order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry"]))}
        half = len(rows) // 2
        print(f"    overall {mean([r['oz'] for r in rows]):+.2f} $/oz per trade")

        print("\n    the range (what demo2 already skips)")
        show("inside a range", [r for r in rows if r["boxed"]], order, half)
        show("outside", [r for r in rows if not r["boxed"]], order, half)

        print("\n    how straight the last 4 hours were")
        show("chop (below 0.25)", [r for r in rows if r["straight"] < 0.25], order, half)
        show("mixed (0.25-0.50)", [r for r in rows if 0.25 <= r["straight"] < 0.50], order, half)
        show("trend (0.50 and up)", [r for r in rows if r["straight"] >= 0.50], order, half)

        print("\n    M3 crosses in the 2 hours before entry")
        show("0-1 crosses", [r for r in rows if r["churn"] <= 1], order, half)
        show("2 crosses", [r for r in rows if r["churn"] == 2], order, half)
        show("3 or more", [r for r in rows if r["churn"] >= 3], order, half)

    print(f"\n{'=' * 104}")
    print("THE BAR (set before this first run): a condition is worth building into the bot only")
    print(f"if its bad group loses money in BOTH halves, on BOTH M3 accounts AND both M5 accounts,")
    print(f"with at least {MIN_GROUP} trades in the group. A group that merely earns LESS is a")
    print("group we keep trading -- that lesson has cost real money here more than once.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
