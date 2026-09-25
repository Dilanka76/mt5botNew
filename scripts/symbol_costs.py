"""What does holding this symbol actually cost? Spread, swap, the lot.

2026-09-25. The Donchian H4 result passes every historical test given to
it -- 6.75 years, an unseen 2020-2022 half, three timeframes, and four
lookbacks that all earn (a plateau, not a spike). One thing is still
missing from the arithmetic: it holds positions for a MEDIAN OF FIVE
DAYS, and `scripts/strategy_lab.py` has been charging no financing at
all. Five nights of swap could take a fifth of that edge, or two thirds
of it. Until this number is real, that strategy is not.

Reading it off the MT5 Specification window is easy to get wrong -- swap
can be quoted in points, in the symbol's currency, or as a percent, and
which one it is changes the answer by a factor of a hundred. So this
asks the terminal directly and converts it into the unit the lab uses:
dollars per OUNCE per night.

    python scripts/symbol_costs.py
    python scripts/symbol_costs.py --account demo2_m3

Read-only: it opens a connection, reads the symbol, and disconnects.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

# MT5's own swap modes, by value. Only the first few appear in practice.
SWAP_MODES = {
    0: "disabled",
    1: "in POINTS",
    2: "in the symbol's base currency",
    3: "as interest on margin currency",
    4: "in the DEPOSIT currency, per lot",
    5: "as current interest (annual %)",
    6: "as open-price interest (annual %)",
    7: "reopen by current price",
    8: "reopen by bid",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo2_m3")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(validate_account_name(args.account))
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        info = mt5.symbol_info(config.symbol)
        tick = mt5.symbol_info_tick(config.symbol)
    finally:
        connector.disconnect()

    if info is None:
        raise SystemExit(f"{config.symbol}: the terminal returned nothing for this symbol")

    mode = getattr(info, "swap_mode", None)
    point = float(info.point)
    contract = float(info.trade_contract_size)
    oz_per_lot = contract          # gold: 100 ounces in a lot

    print("=" * 84)
    print(f"WHAT {config.symbol} COSTS   (account {args.account})")
    print("=" * 84)
    print(f"  contract size       {contract:g} ounces per lot")
    print(f"  point               {point:g}")
    print(f"  minimum lot         {info.volume_min:g}   step {info.volume_step:g}")
    if tick is not None and tick.ask and tick.bid:
        print(f"  spread right now    ${tick.ask - tick.bid:.2f} per ounce "
              f"({info.spread} points)")
    print(f"  swap mode           {mode} -- {SWAP_MODES.get(mode, 'unknown')}")
    print(f"  swap long  (raw)    {info.swap_long:+g}")
    print(f"  swap short (raw)    {info.swap_short:+g}")
    print(f"  three-day swap on   {getattr(info, 'swap_rollover3days', '?')} "
          f"(0=Sun 1=Mon ... 3=Wed) -- that day charges triple")

    print("\n  IN THE LAB'S UNIT -- dollars per OUNCE per night")
    if mode == 1:          # points
        long_oz, short_oz = info.swap_long * point, info.swap_short * point
        note = "converted: raw points x point size"
    elif mode in (2, 4):   # per lot, in currency
        long_oz, short_oz = info.swap_long / oz_per_lot, info.swap_short / oz_per_lot
        note = f"converted: per-lot charge / {oz_per_lot:g} ounces"
    else:
        long_oz = short_oz = None
        note = ("this swap mode is a percentage or a reopen rule, which cannot be turned "
                "into a flat per-night figure without the price and the broker's formula")

    if long_oz is None:
        print(f"    cannot convert -- {note}")
        print("    Hold a 0.01-lot position overnight on the demo and read the real charge "
              "from the trade's Swap column instead.")
    else:
        print(f"    long   {long_oz:+.4f} $/oz per night")
        print(f"    short  {short_oz:+.4f} $/oz per night")
        print(f"    ({note})")
        print("\n  Feed them straight into the lab:")
        print(f"    python scripts\\strategy_lab.py --strategy donchian --timeframe H4 \\")
        print(f"        --since \"2020-01-01 00:00:00\" --max-hold-hours 2000 \\")
        print(f"        --swap-long {long_oz:.4f} --swap-short {short_oz:.4f}")

    print("\n" + "=" * 84)
    print("A caution: a swap rate is not a constant. Brokers change it, and one day a week")
    print("charges triple. Treat any result built on today's number as an estimate, and")
    print("re-read it before trusting a strategy that holds positions for days.")


if __name__ == "__main__":
    main()
