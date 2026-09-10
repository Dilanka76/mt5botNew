"""What does the broker actually allow for stop placement?

Needed before building the TP-runner rule (scripts/simulate_tp_runner.py).
That simulation assumed a stop can be parked exactly at the take-profit
price and filled there. Two broker realities may break that:

  - STOPS LEVEL: the minimum distance MT5 requires between the current
    price and any stop or limit order. A stop requested closer than this
    is REJECTED. If it is larger than zero on XAUUSDp, the stop cannot sit
    at the exact TP price and must be buffered below it -- which means
    giving back that buffer on every trade that turns around, and changes
    the arithmetic of every variant tested.
  - FREEZE LEVEL: how close to the current price an existing order becomes
    frozen and can no longer be modified or cancelled. This decides whether
    the broker take-profit can even be removed as price approaches it.

Also reports the live spread, since the new exit is a stop (a market
order once triggered, so it pays the spread and can slip) whereas the
take-profit it replaces is a limit order that cannot fill worse than its
price.

    python scripts/check_broker_limits.py --account demo1_m3

Read-only: reads symbol and tick info. Places no orders.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.formatting import usd
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo1_m3")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    account = validate_account_name(args.account)
    config = load_config(account)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        info = mt5.symbol_info(config.symbol)
        tick = mt5.symbol_info_tick(config.symbol)
        acct = mt5.account_info()
    finally:
        connector.disconnect()

    if info is None:
        print(f"symbol_info({config.symbol}) returned None -- symbol not available.")
        return

    point = info.point
    stops_pts = info.trade_stops_level
    freeze_pts = info.trade_freeze_level
    stops_usd = stops_pts * point
    freeze_usd = freeze_pts * point

    print(f"{account}: {config.symbol}   (TP ${config.take_profit_usd:.2f}, stop {usd(config.stop_loss_usd)})")
    print(f"  point size          : {point}")
    print(f"  STOPS level         : {stops_pts} points  =  ${stops_usd:.2f} of price")
    print(f"  FREEZE level        : {freeze_pts} points  =  ${freeze_usd:.2f} of price")
    if tick is not None:
        print(f"  current bid/ask     : {tick.bid} / {tick.ask}   spread ${tick.ask - tick.bid:.2f}")
    print(f"  volume min/step/max : {info.volume_min} / {info.volume_step} / {info.volume_max}")

    # Margin mode decides whether the split design is even possible: on a
    # NETTING account two same-direction orders merge into a single
    # position with one stop and one target, so the "half banked at a
    # guaranteed limit fill, half left running" structure cannot exist.
    print()
    if acct is None:
        print("  MARGIN MODE      : account_info() returned None -- could not determine.")
    else:
        hedging = acct.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        print(f"  MARGIN MODE      : {'HEDGING' if hedging else 'NETTING'} "
              f"(raw margin_mode={acct.margin_mode})")
        if hedging:
            print("    -> Two same-direction orders stay as SEPARATE positions, each with its")
            print("       own stop and target. The split design works as intended: half can")
            print("       hold a real broker take-profit (a guaranteed limit fill) while the")
            print("       other half runs on a trailing stop.")
        else:
            print("    -> Two same-direction orders MERGE into one position with a single")
            print("       stop and target. The split as designed is NOT possible. The half")
            print("       would have to be taken by partial close at market, which the bot")
            print("       sends on its polling loop -- so it is NOT a guaranteed fill and")
            print("       gives up exactly the crash protection the split was chosen for.")

    print()
    if stops_pts == 0:
        print("  STOPS level is 0 -- the broker imposes no minimum distance, so a stop")
        print("  CAN be placed at (or extremely near) the take-profit price. The rule is")
        print("  implementable as simulated. Slippage on the stop still applies.")
    else:
        print(f"  STOPS level is ${stops_usd:.2f}, so a stop cannot sit closer than that to")
        print(f"  the current price. Locking at the exact TP is NOT possible -- the lock")
        print(f"  must be at least ${stops_usd:.2f} below it, which is given back on every")
        print(f"  trade that turns around. Re-run simulate_tp_runner.py with a lock of")
        print(f"  ${config.take_profit_usd - stops_usd:.2f} or lower before trusting the earlier numbers.")
    if freeze_pts > 0:
        print(f"  FREEZE level ${freeze_usd:.2f}: the broker take-profit cannot be modified or")
        print(f"  removed once price is within that distance of it, so it must be cancelled")
        print(f"  EARLIER than the moment price arrives -- design accordingly.")


if __name__ == "__main__":
    main()
