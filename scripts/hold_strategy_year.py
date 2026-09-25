"""STAGE 1: hold through the opposite cross, a full year, one trade at a time.

2026-09-24. The user, watching the live chart: "in the consolidation,
sideway, I can't grab the win because the opposite cross comes and I lose
the trade." scripts/hold_through_cross_test.py priced that on real trades
and holding beat reversing on 3 of 4 accounts (+$3,601 over a month) --
but that test let every chain's first trade be held INDEPENDENTLY, when
one held trade really occupies the account for up to 48 hours. It counted
winners that could not have existed at the same time.

This removes that flaw completely. It is a sequential simulation: one
position at a time, and a cross that fires while a trade is open is
IGNORED, exactly as the real bot would have to.

THE RULE, the live entry with the live numbers -- nothing here is chosen
by me, so nothing here is tuned:

  entry      EMA13/21 cross with the candle CLOSING on the new side,
             entered at the next candle's open. Only when flat.
  target     $6, or $8 when the M15 EMA13/21 trend agrees with the
             direction -- the live2 rule exactly.
  exit       the target, the $30 broker backstop, or 48 hours. THE
             OPPOSITE CROSS IS IGNORED. That is the whole change.
  costs      0.18 $/oz a trade: $6/lot commission plus a $0.12 spread.
  harsh      a candle touching both target and backstop counts as the
             BACKSTOP, because candles cannot say which came first.

WHAT IT IS MEASURED AGAINST: the same entries with the live exit lost
-$417 before commission over ten months on M3 (the year backtest), about
-$927 after. Anything that does not clearly beat that is not interesting.

THE BAR, set before the first run, same as every other candidate here:
  1. profitable after costs over the year,
  2. profitable in BOTH halves,
  3. at least 100 trades,
  4. a drawdown a $300 account could live through.

    python scripts/hold_strategy_year.py --since "2025-11-01 00:00:00"
    python scripts/hold_strategy_year.py --timeframe M5 --target 8 --htf-target 10 --backstop 35
    (add --offset-hours 3 when the market is closed)

Read-only. No engine, no config, no bot, no order.
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
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

COSTS_PER_OZ = 0.18
MIN_TRADES = 100


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo2_m3", help="only for the symbol and connection")
    p.add_argument("--timeframe", default="M3")
    p.add_argument("--since", default="2025-11-01 00:00:00", help="true UTC")
    p.add_argument("--until", default=None)
    p.add_argument("--target", type=float, default=6.0, help="$/oz against the M15 trend")
    p.add_argument("--htf-target", type=float, default=8.0, help="$/oz running with it")
    p.add_argument("--backstop", type=float, default=30.0, help="$/oz; 0 = none")
    p.add_argument("--max-hold-hours", type=float, default=48.0)
    p.add_argument("--exit-mode", default="hold",
                   choices=("hold", "swap", "trend-hold", "range-hold"),
                   help="hold = ignore the opposite cross (the user's idea); "
                        "swap = the live rule, close and reverse on it; "
                        "trend-hold = hold only when the trade runs WITH the M15 trend, "
                        "otherwise take the small loss on the cross; "
                        "range-hold = hold only when the entry is inside a range")
    p.add_argument("--fast-ema", type=int, default=13,
                   help="the faster line of the cross (13 is the live rule)")
    p.add_argument("--slow-ema", type=int, default=21,
                   help="the slower line (21 is the live rule)")
    p.add_argument("--offset-hours", type=float, default=None)
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def simulate(df, target, htf_target, backstop, max_hold, exit_mode="hold"):
    """One position at a time. A cross confirms at a candle's CLOSE and is
    entered at the NEXT candle's open, so no price here is one the rule
    could not have seen.

    `exit_mode` decides, AT ENTRY, whether this trade ignores the opposite
    cross ("hold") or closes on it ("swap"). The two hybrids choose per
    trade from something known before the entry: the M15 trend, or whether
    the entry sits inside a range. Nothing here reads a later candle."""
    o = df["open"].tolist()
    hi = df["high"].tolist()
    lo = df["low"].tolist()
    cl = df["close"].tolist()
    e13 = df["ema13"].tolist()
    e21 = df["ema21"].tolist()
    trend = df["htf_trend"].tolist()
    times = list(df.index)
    boxed = (df["range_state"].tolist() if "range_state" in getattr(df, "columns", [])
             else [float("nan")] * len(times))

    trades: list = []
    position = None

    # Every candle from the second is examined. Exits must reach the LAST
    # candle -- stopping at len-1 to leave room for the entry's "next
    # candle open" silently dropped any exit on the final bar (caught by
    # tests/test_hold_strategy.py before this was ever run on real data).
    for i in range(1, len(df)):
        if position is not None:
            sign, stop, tp = position["sign"], position["stop"], position["tp"]
            hit_stop = stop is not None and ((lo[i] <= stop) if sign > 0 else (hi[i] >= stop))
            hit_tp = (hi[i] >= tp) if sign > 0 else (lo[i] <= tp)
            # The live exit: this candle CLOSED with the EMAs against us.
            against = (e13[i] < e21[i]) if sign > 0 else (e13[i] > e21[i])
            out = why = None
            if hit_stop:                       # the harsh assumption, on purpose
                out, why = stop, "backstop"
            elif hit_tp:
                out, why = tp, "target"
            elif against and not position["hold"]:
                out, why = cl[i], "opposite cross"
            elif times[i] - position["opened"] >= max_hold:
                out, why = cl[i], "48h"
            if out is None:
                continue
            gross = (out - position["entry"]) * sign
            trades.append({"opened": position["opened"], "closed": times[i],
                           "oz": gross - COSTS_PER_OZ, "reason": why,
                           "direction": "BUY" if sign > 0 else "SELL"})
            position = None
            # No `continue`: a cross exit happens ON the crossing candle, so
            # the entry check below can reverse into it immediately -- which
            # is exactly what the live engine does.

        # a CONFIRMED cross: the lines crossed and this candle closed on the new side
        up = e13[i] > e21[i] and e13[i - 1] <= e21[i - 1]
        down = e13[i] < e21[i] and e13[i - 1] >= e21[i - 1]
        if not (up or down) or i + 1 >= len(df):
            continue                      # no next candle to enter on
        sign = 1 if up else -1
        entry = o[i + 1]
        agrees = trend[i] == trend[i] and ((trend[i] > 0) == (sign > 0))
        size = htf_target if agrees else target
        in_range = boxed[i] == 1.0
        hold = {"hold": True, "swap": False,
                "trend-hold": agrees, "range-hold": in_range}[exit_mode]
        position = {"sign": sign, "entry": entry, "opened": times[i + 1], "hold": hold,
                    "tp": entry + sign * size,
                    "stop": (entry - sign * backstop) if backstop else None}
    return trades


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    until = (datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
             if args.until else datetime.now(timezone.utc))
    max_hold = timedelta(hours=args.max_hold_hours)

    config = load_config(validate_account_name(args.account))
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        df = get_ohlc_range(connector, config.symbol, args.timeframe,
                            since - timedelta(days=3), until, offset)
        m15 = get_ohlc_range(connector, config.symbol, "M15",
                             since - timedelta(days=3), until, offset)
    finally:
        connector.disconnect()

    # The pair is a parameter, not the config's, so any EMA cross can be
    # tested -- the columns keep the names the simulator already reads.
    df["ema13"] = df["close"].ewm(span=args.fast_ema, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=args.slow_ema, adjust=False).mean()
    m15 = compute_emas(m15, config.ema_periods)
    # The M15 verdict is only known once its candle has CLOSED, so stamp it
    # at close time and carry it forward -- the same treatment the range
    # filter gives its levels.
    import pandas as pd
    trend = pd.Series((m15["ema13"] > m15["ema21"]).map({True: 1.0, False: -1.0}).to_numpy(),
                      index=m15.index + pd.Timedelta(minutes=15))
    trend = trend[~trend.index.duplicated(keep="last")].sort_index()
    bar_minutes = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60}[args.timeframe]
    df["htf_trend"] = trend.reindex(df.index + pd.Timedelta(minutes=bar_minutes),
                                    method="ffill").to_numpy()
    df = df[df.index >= since]

    if args.exit_mode == "range-hold":
        from bot.indicators.range_filter import compute_range
        df = compute_range(df, m15, 15, bar_minutes, 16, 2, 0.35)

    trades = simulate(df, args.target, args.htf_target, args.backstop, max_hold, args.exit_mode)

    print("=" * 92)
    print("HOLD THROUGH THE CROSS -- one trade at a time, a cross while in a trade is IGNORED")
    print(f"{config.symbol} {args.timeframe}   {since:%Y-%m-%d} to {until:%Y-%m-%d}   "
          f"{len(df):,} candles   EMA{args.fast_ema}/{args.slow_ema} cross")
    print(f"exit mode: {args.exit_mode}")
    print(f"target ${args.target:g} / ${args.htf_target:g} with the M15 trend   "
          f"backstop {('$' + format(args.backstop, 'g')) if args.backstop else 'NONE'}   "
          f"max hold {args.max_hold_hours:g}h   costs {COSTS_PER_OZ:.2f} $/oz")
    print("=" * 92)

    if not trades:
        print("\n  no trades")
        return

    half = len(trades) // 2
    first, second = [t["oz"] for t in trades[:half]], [t["oz"] for t in trades[half:]]
    wins = [t for t in trades if t["oz"] > 0]
    losses = [t["oz"] for t in trades if t["oz"] <= 0]
    total = sum(t["oz"] for t in trades)

    print(f"\n  trades              {len(trades)}")
    print(f"  won                 {len(wins)} ({100 * len(wins) / len(trades):.1f}%)")
    print(f"  net                 {mean([t['oz'] for t in trades]):+.3f} $/oz per trade, "
          f"{total:+.2f} $/oz in total")
    print(f"  at 0.01 lots        {money(total)}")
    print(f"  halves              {mean(first):+.3f} / {mean(second):+.3f} $/oz")
    if wins and losses:
        print(f"  average win / loss  {mean([t['oz'] for t in wins]):+.2f} / {mean(losses):+.2f}"
              f"   ratio {abs(mean([t['oz'] for t in wins]) / mean(losses)):.2f}")
    for reason in ("target", "backstop", "opposite cross", "48h"):
        sel = [t for t in trades if t["reason"] == reason]
        if sel:
            print(f"  closed by {reason:<9} {len(sel):>4} ({100 * len(sel) / len(trades):>4.1f}%)"
                  f"  {sum(t['oz'] for t in sel):+9.2f} $/oz")
    held = [(t["closed"] - t["opened"]).total_seconds() / 3600 for t in trades]
    print(f"  time in a trade     median {statistics.median(held):.1f}h, longest {max(held):.1f}h")

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
    ok_total, ok_halves = total > 0, (mean(first) > 0 and mean(second) > 0)
    ok_n = len(trades) >= MIN_TRADES
    print("THE BAR (set before this run): profitable after costs, in BOTH halves, "
          f"{MIN_TRADES}+ trades.")
    print(f"  after costs      {'PASS' if ok_total else 'FAIL'}   ({total:+.2f} $/oz)")
    print(f"  both halves      {'PASS' if ok_halves else 'FAIL'}   "
          f"({mean(first):+.3f} / {mean(second):+.3f})")
    print(f"  enough trades    {'PASS' if ok_n else 'FAIL'}   ({len(trades)})")
    print(f"\n  VERDICT: {'PASSES STAGE 1 -- next is Stage 2, trying to break it' if (ok_total and ok_halves and ok_n) else 'FAILS -- it joins the other 30'}")
    print("\nWhat this canNOT tell you: candles hide the order of moves inside one candle (scored")
    print("as the backstop here), the spread is assumed constant, and a real fill is not")
    print("guaranteed at the modelled price in a fast market.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
