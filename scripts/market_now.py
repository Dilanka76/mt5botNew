"""What is gold doing RIGHT NOW -- trending, or boxed in?

User, 2026-09-23: "the market is clearly running without consolidation
now... after this momentum I think it will range, and then we give back
the gain and we lose in the range -- that's the problem."

So this is the live picture, read with the SAME range detector the bot
uses (bot/indicators/range_filter.py), plus the things that decide how a
trend behaves when it dies:

  the ranges       the M15 ceiling and floor the bot sees this minute,
                   and whether price sits inside them (the bot's own
                   in_range verdict, the one it acts on for demo2)
  the trend        EMA13 vs EMA21 on M15 and H1, and how far apart
  the travel       how much of the last 4h / 24h movement was in one
                   direction: near 1.0 is a clean trend, near 0 is chop
  the churn        how many M3 and M5 crosses fired in the last 2 hours
                   -- the bot flips on each one, so this is what a range
                   costs in trades

It reads the market only. No trades, no config, no bot state.

    python scripts/market_now.py
    python scripts/market_now.py --account live2_m3 --offset-hours 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.indicators.range_filter import in_range, range_levels, with_atr
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="live2_m3", type=validate_account_name,
                   help="only for the symbol and the EMA periods")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def travel(df, hours: int, bar_minutes: int) -> tuple[float, float, float]:
    """(net move, total movement, straightness 0-1) over the last `hours`."""
    n = max(2, int(hours * 60 / bar_minutes))
    w = df.tail(n)
    net = float(w["close"].iloc[-1] - w["open"].iloc[0])
    total = float((w["close"] - w["open"]).abs().sum())
    return net, total, (abs(net) / total if total else 0.0)


def crosses(df, hours: int, bar_minutes: int) -> int:
    n = max(2, int(hours * 60 / bar_minutes))
    side = (df["ema13"] > df["ema21"]).tail(n)
    return int((side != side.shift(1)).iloc[1:].sum())


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    now = datetime.now(timezone.utc)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        frames = {tf: get_ohlc_range(connector, config.symbol, tf,
                                     now - timedelta(days=4), now, offset)
                  for tf in ("M3", "M5", "M15", "H1")}
        tick = connector.get_tick(config.symbol) if hasattr(connector, "get_tick") else None
    finally:
        connector.disconnect()

    for tf in frames:
        frames[tf] = compute_emas(frames[tf], config.ema_periods)
    m15, h1 = frames["M15"], frames["H1"]
    price = float(m15["close"].iloc[-1]) if tick is None else float(tick.bid)

    print("=" * 84)
    print(f"GOLD RIGHT NOW   {config.symbol}   {datetime.now(COLOMBO):%d %b %H:%M} Colombo")
    print("=" * 84)
    print(f"  price {price:.2f}")

    print("\n  TREND")
    for name, df in (("M15", m15), ("H1", h1)):
        last = df.iloc[-1]
        gap = float(last["ema13"]) - float(last["ema21"])
        side = "UP" if gap > 0 else "DOWN"
        print(f"    {name:<4} EMA13 {'above' if gap > 0 else 'below'} EMA21 by ${abs(gap):.2f}"
              f"   -> {side}")

    print("\n  HOW STRAIGHT THE MOVE HAS BEEN")
    for hours, (df, bar) in ((4, (frames["M15"], 15)), (24, (frames["H1"], 60))):
        net, total, straight = travel(df, hours, bar)
        shape = "TRENDING" if straight >= 0.5 else ("mixed" if straight >= 0.25 else "CHOPPY")
        print(f"    last {hours:>2}h   net {net:>+8.2f}   movement {total:>7.2f}   "
              f"straightness {straight:.2f}   {shape}")

    print("\n  THE BOT'S OWN RANGE CHECK (M15, the rule demo2 skips on)")
    closed = with_atr(m15).iloc[:-1]                 # the forming candle is not closed
    window = closed.tail(16)
    ceiling, floor = range_levels(window) if len(window) == 16 else (None, None)
    state = 1.0 if ceiling is not None else 0.0
    verdict = in_range(price, state, ceiling if ceiling else float("nan"),
                       floor if floor else float("nan"),
                       type("cfg", (), {"enabled": True})())
    if ceiling is None:
        print("    no range: the last 4 hours have no ceiling and floor touched twice")
        print("    -> a cross NOW would be taken by every account")
    else:
        width = ceiling - floor
        where = (price - floor) / width if width else 0.0
        print(f"    ceiling {ceiling:.2f}   floor {floor:.2f}   width ${width:.2f}")
        print(f"    price is {100 * where:.0f}% of the way up that box")
        print(f"    -> a cross NOW would be {'SKIPPED by demo2' if verdict else 'taken'}"
              f" (live2 records it either way)")

    print("\n  CHURN -- how often the lines crossed in the last 2 hours")
    for tf, bar in (("M3", 3), ("M5", 5)):
        n = crosses(frames[tf], 2, bar)
        print(f"    {tf:<4} {n} cross(es)"
              + ("   <- the bot flips on each one" if n >= 3 else ""))

    print(f"\n{'=' * 84}")
    print("A trend dies into a range: the crosses keep firing while price goes nowhere.")
    print("That is what the range check is being tested to catch -- watch it flip from")
    print("'no range' to a ceiling and floor as the move runs out.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
