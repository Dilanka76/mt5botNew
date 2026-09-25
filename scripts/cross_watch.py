"""Where did the EMAs actually cross? A read-only look at what the bot sees.

2026-09-25. demo2_test was started on M1 with an EMA5/EMA9 cross and sat
IDLE with an empty decision log. Either the market genuinely has not
crossed, or the bot is not seeing what we think it is. Guessing between
those two is how hours get wasted, so this asks the same candles the bot
reads and prints every cross in them.

It computes the EMAs exactly as bot/indicators/ema.py does (pandas ewm,
adjust=False) and marks a cross the same way the engine does: the lines
swapped sides AND the candle closed on the new side.

    python scripts/cross_watch.py --account demo2_test
    python scripts/cross_watch.py --account demo2_test --fast 5 --slow 9 --candles 300

Read-only: connects, reads candles, disconnects. Touches no bot.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

sys.path.insert(0, ".")

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo2_test")
    p.add_argument("--fast", type=int, default=None, help="defaults to the account's ema mid")
    p.add_argument("--slow", type=int, default=None, help="defaults to the account's ema slow")
    p.add_argument("--candles", type=int, default=200)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(validate_account_name(args.account))
    fast = args.fast if args.fast is not None else config.ema_periods.mid
    slow = args.slow if args.slow is not None else config.ema_periods.slow

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc(connector, config.symbol, config.timeframe, args.candles)
    finally:
        connector.disconnect()

    df["f"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["s"] = df["close"].ewm(span=slow, adjust=False).mean()
    above = df["f"] > df["s"]

    # The last row is the candle still forming; the bot only judges closed ones.
    closed = df.iloc[:-1]
    print("=" * 84)
    print(f"EMA{fast}/EMA{slow} on {config.symbol} {config.timeframe}   "
          f"{len(closed)} closed candles")
    print(f"from {closed.index[0]} to {closed.index[-1]}  (broker clock)")
    print("=" * 84)

    crosses = []
    for i in range(1, len(closed)):
        if bool(above.iloc[i]) != bool(above.iloc[i - 1]):
            crosses.append((closed.index[i], "BUY" if above.iloc[i] else "SELL",
                            float(closed["f"].iloc[i]), float(closed["s"].iloc[i])))

    if not crosses:
        print("\n  NO CROSSES AT ALL in this window.")
        print("  The bot is right to be idle -- there is nothing to enter.")
    else:
        span_minutes = (closed.index[-1] - closed.index[0]).total_seconds() / 60
        print(f"\n  {len(crosses)} crosses in {span_minutes:.0f} minutes "
              f"-- one every {span_minutes / len(crosses):.1f} minutes on average\n")
        for when, side, f, s in crosses[-15:]:
            print(f"    {when}  {side:<4}  EMA{fast} {f:.2f}   EMA{slow} {s:.2f}")
        if len(crosses) > 15:
            print(f"    ... and {len(crosses) - 15} earlier")

    last = closed.iloc[-1]
    gap = float(last["f"]) - float(last["s"])
    print(f"\n  right now: EMA{fast} {float(last['f']):.2f}, EMA{slow} {float(last['s']):.2f}, "
          f"gap ${gap:+.2f} -- EMA{fast} is {'ABOVE' if gap > 0 else 'BELOW'}")
    if crosses:
        since = closed.index[-1] - crosses[-1][0]
        print(f"  the last cross was {since.total_seconds() / 60:.0f} minutes ago "
              f"({crosses[-1][1]})")

    print("\n" + "=" * 84)
    print("If crosses ARE happening and the bot's decision log is empty, the bot is not")
    print("seeing these candles and that is a bug to find -- not something to wait out.")


if __name__ == "__main__":
    main()
