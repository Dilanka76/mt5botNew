"""TRADE the range instead of avoiding it. A year, on real candles.

2026-09-24. The EMA13/21 confirmed-cross rule is dead -- tested on M3,
M5, M15 and H1 over a year, 5,600 trades, negative at every timeframe
(see the year backtest reports). Its shape never changed: about 51% wins
and an average loss 11% bigger than the average win. A capped target
with an uncapped loss cannot be rescued by any filter, and 29 were tried.

The user has said five times that gold spends most of its time going
sideways, and he is right -- the consolidation filter proved those trades
exist, it just could not profit by SKIPPING them. So this tests the
opposite idea, which this project has never tried: **trade the range on
purpose**, with the opposite economics -- a small stop, a large target,
and a win rate that is allowed to be low.

THE RULE, written before the first run and not tuned afterwards:

  the range   the SAME frozen definition the filter already uses --
              bot/indicators/range_filter.py on M15, 16 closed candles
              (4 hours), fractal 2, two touches within 0.35 ATR. No new
              definition, no new parameters.
  entry       flat, a range exists, and an M3 candle's high reaches the
              CEILING -> SELL at the ceiling. Its low reaches the
              FLOOR -> BUY at the floor. Both in one candle -> no trade,
              it is ambiguous.
  target      the OPPOSITE side of the range.
  stop        a quarter of the range height beyond the level.
  so          risk 0.25H to make 1.0H -- four to one. It only needs to
              be right more than about a quarter of the time.
  re-entry    after a stop-out, price must come back INSIDE the range
              before another trade is taken, or one broken level would
              be sold over and over.
  time        unresolved after 48 hours -> closed at that candle's price.

HOW IT IS SCORED, deliberately harshly:
  - a candle that touches BOTH the stop and the target counts as the
    STOP. Candles cannot say which came first, and the flattering
    assumption is how backtests lie.
  - every trade pays 0.18 $/oz: $0.06 commission ($6/lot) plus a $0.12
    spread, the real live2 numbers.
  - outcomes are in $/oz, so lot size tilts nothing.

THE BAR, set before the first run. This is worth building only if it:
  1. makes money over the year AFTER costs,
  2. makes money in BOTH halves,
  3. has at least 100 trades,
  4. and its worst drawdown at 0.01 lots is something $300 survives.
Anything less and it goes in the pile with the other 29.

    python scripts/range_strategy_test.py --since "2025-11-01 00:00:00"
    python scripts/range_strategy_test.py --offset-hours 3      (weekends)

Read-only. No engine, no config, no bot. If it passes, THEN we build it.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.range_filter import compute_range
from bot.mt5_connector import MT5Connector

COSTS_PER_OZ = 0.18          # $6/lot commission = $0.06/oz, plus a $0.12 spread
STOP_FRACTION = 0.25         # of the range height, beyond the level
MAX_HOLD = timedelta(hours=48)
LOOKBACK, FRACTAL, TOLERANCE = 16, 2, 0.35      # the frozen range definition
MIN_TRADES = 100


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo2_m3", help="only for the symbol and the connection")
    p.add_argument("--since", default="2025-11-01 00:00:00", help="true UTC")
    p.add_argument("--until", default=None, help="true UTC; default now")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def simulate(df) -> list:
    """One position at a time, every price read from the candle it belongs
    to. A trade opened on candle i is managed from candle i+1 onward, so
    nothing here uses a price the rule could not have seen."""
    trades: list = []
    position = None
    waiting_for_reentry = False          # after a stop-out, until price is back inside

    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    states = df["range_state"].tolist()
    ceilings = df["range_ceiling"].tolist()
    floors = df["range_floor"].tolist()
    times = list(df.index)

    for i in range(len(df)):
        hi, lo = highs[i], lows[i]

        if position is not None:
            sign = position["sign"]
            stop, target = position["stop"], position["target"]
            hit_stop = (hi >= stop) if sign < 0 else (lo <= stop)
            hit_target = (lo <= target) if sign < 0 else (hi >= target)
            exit_price = reason = None
            if hit_stop:                       # the harsh assumption, on purpose
                exit_price, reason = stop, "stop"
            elif hit_target:
                exit_price, reason = target, "target"
            elif times[i] - position["opened"] >= MAX_HOLD:
                exit_price, reason = closes[i], "time"
            if exit_price is not None:
                # A SELL earns when price falls, a BUY when it rises.
                gross = ((position["entry"] - exit_price) if sign < 0
                         else (exit_price - position["entry"]))
                trades.append({"opened": position["opened"], "closed": times[i],
                               "direction": "SELL" if sign < 0 else "BUY",
                               "oz": gross - COSTS_PER_OZ, "reason": reason,
                               "height": position["height"]})
                waiting_for_reentry = reason == "stop"
                position = None
            continue

        state, ceiling, floor = states[i], ceilings[i], floors[i]
        if state != 1.0 or ceiling != ceiling or floor != floor:
            continue
        height = ceiling - floor
        if height <= 0:
            continue

        if waiting_for_reentry:
            if floor <= closes[i] <= ceiling:
                waiting_for_reentry = False
            continue

        touch_ceiling, touch_floor = hi >= ceiling, lo <= floor
        if touch_ceiling and touch_floor:
            continue                                   # ambiguous inside one candle
        if touch_ceiling:
            position = {"sign": -1, "entry": ceiling, "target": floor,
                        "stop": ceiling + STOP_FRACTION * height,
                        "opened": times[i], "height": height}
        elif touch_floor:
            position = {"sign": 1, "entry": floor, "target": ceiling,
                        "stop": floor - STOP_FRACTION * height,
                        "opened": times[i], "height": height}
    return trades


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    until = (datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
             if args.until else datetime.now(timezone.utc))

    config = load_config(validate_account_name(args.account))
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        m3 = get_ohlc_range(connector, config.symbol, "M3", since - timedelta(days=3), until, offset)
        m15 = get_ohlc_range(connector, config.symbol, "M15", since - timedelta(days=3), until, offset)
    finally:
        connector.disconnect()

    df = compute_range(m3, m15, 15, 3, LOOKBACK, FRACTAL, TOLERANCE)
    df = df[df.index >= since]
    trades = simulate(df)

    print("=" * 92)
    print("TRADE THE RANGE -- sell the ceiling, buy the floor, stop a quarter of the way out")
    print(f"{config.symbol}   {since:%Y-%m-%d} to {until:%Y-%m-%d}   {len(df):,} M3 candles")
    print(f"costs charged: {COSTS_PER_OZ:.2f} $/oz every trade.  A candle touching both the stop")
    print("and the target is scored as the STOP.")
    print("=" * 92)

    if not trades:
        print("\n  no trades at all -- the range definition never produced a touch")
        return

    half = len(trades) // 2
    first = [t["oz"] for t in trades[:half]]
    second = [t["oz"] for t in trades[half:]]
    wins = [t for t in trades if t["oz"] > 0]
    total = sum(t["oz"] for t in trades)

    print(f"\n  trades              {len(trades)}")
    print(f"  won                 {len(wins)} ({100 * len(wins) / len(trades):.1f}%)")
    print(f"  net                 {mean([t['oz'] for t in trades]):+.3f} $/oz per trade, "
          f"{total:+.2f} $/oz in total")
    print(f"  at 0.01 lots        {money(total)} over the period")
    print(f"  halves              {mean(first):+.3f} / {mean(second):+.3f} $/oz")
    if wins:
        losses = [t["oz"] for t in trades if t["oz"] <= 0]
        print(f"  average win / loss  {mean([t['oz'] for t in wins]):+.2f} / "
              f"{mean(losses):+.2f} $/oz"
              + (f"   ratio {abs(mean([t['oz'] for t in wins]) / mean(losses)):.2f}"
                 if losses else ""))
    for reason in ("target", "stop", "time"):
        sel = [t for t in trades if t["reason"] == reason]
        if sel:
            print(f"  closed by {reason:<9} {len(sel):>4} ({100 * len(sel) / len(trades):>4.1f}%)  "
                  f"{sum(t['oz'] for t in sel):+8.2f} $/oz")

    balance = peak = worst = 0.0
    streak = longest = 0
    for t in trades:
        balance += t["oz"]
        peak = max(peak, balance)
        worst = max(worst, peak - balance)
        streak = 0 if t["oz"] > 0 else streak + 1
        longest = max(longest, streak)
    print(f"  worst drawdown      -{worst:.2f} $/oz  ({money(-worst)} at 0.01 lots)")
    print(f"  longest losing run  {longest} trades")

    months: OrderedDict = OrderedDict()
    for t in trades:
        m = months.setdefault(t["closed"].strftime("%Y-%m"), {"n": 0, "oz": 0.0})
        m["n"] += 1
        m["oz"] += t["oz"]
    red = sum(1 for m in months.values() if m["oz"] < 0)
    print(f"\n  month by month -- {red} of {len(months)} lost money")
    for name, m in months.items():
        print(f"    {name}   {m['n']:>4} trades  {m['oz']:+8.2f} $/oz")

    print("\n" + "=" * 92)
    passed = (total > 0 and mean(first) > 0 and mean(second) > 0 and len(trades) >= MIN_TRADES)
    print(f"THE BAR (set before this run): profitable after costs, in BOTH halves, "
          f"{MIN_TRADES}+ trades.")
    print(f"  after costs      {'PASS' if total > 0 else 'FAIL'}   ({total:+.2f} $/oz)")
    print(f"  both halves      {'PASS' if mean(first) > 0 and mean(second) > 0 else 'FAIL'}"
          f"   ({mean(first):+.3f} / {mean(second):+.3f})")
    print(f"  enough trades    {'PASS' if len(trades) >= MIN_TRADES else 'FAIL'}   ({len(trades)})")
    print(f"\n  VERDICT: {'WORTH BUILDING -- next step is a forward test, not a live account' if passed else 'FAILS -- it goes in the pile with the other 29'}")
    print("\nWhat this canNOT tell you: candles hide the order of moves inside one candle (scored")
    print("as the stop here, the harsh way), the spread is assumed constant, and a real fill at a")
    print("limit level is not guaranteed in a fast market.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
