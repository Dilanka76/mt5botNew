"""One-off: pulls REAL tick-by-tick bid data for a specific trade's
lifetime and replays the exact breakeven-trigger check
(bot/strategy/state_machine_dual_cross_confirmed_swap_adx.py's
`favorable = entry_price - tick.bid` for SELL, or `tick.bid - entry_price`
for BUY) against every real tick -- to settle, with certainty, whether a
near-miss on the 1-minute candle's OHLC was a genuine near-miss on the
real tick stream too, or whether the live bid stream actually crossed the
trigger and something else explains why breakeven didn't arm.

    python scripts/check_breakeven_near_miss.py --account demo1_m1 --direction SELL --entry 4456.94 --trigger 4.5 --from "2026-08-31 11:41:00" --to "2026-08-31 12:07:00"

--from/--to are TRUE UTC. Connects to MT5 only to read tick history, never
touches live/demo trading.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--account", required=True, type=validate_account_name)
parser.add_argument("--direction", required=True, choices=["BUY", "SELL"])
parser.add_argument("--entry", required=True, type=float)
parser.add_argument("--trigger", required=True, type=float, help="breakeven_trigger_usd for this account")
parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
args = parser.parse_args()

dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
config = load_config(args.account)

connector = MT5Connector(config.mt5)
connector.connect()
try:
    ticks = mt5.copy_ticks_range(config.symbol, dt_from, dt_to, mt5.COPY_TICKS_ALL)
finally:
    connector.disconnect()

if ticks is None or len(ticks) == 0:
    print(f"No ticks returned for this window: {mt5.last_error()}")
    sys.exit(1)

best_favorable = float("-inf")
best_tick = None
crossed_at = None

for t in ticks:
    bid = float(t["bid"])
    if bid == 0.0:
        continue
    favorable = (args.entry - bid) if args.direction == "SELL" else (bid - args.entry)
    if favorable > best_favorable:
        best_favorable = favorable
        best_tick = t
    if favorable >= args.trigger and crossed_at is None:
        crossed_at = t

print(f"account={args.account} direction={args.direction} entry={args.entry} trigger=${args.trigger}")
print(f"{len(ticks)} real ticks in window\n")

best_time = datetime.fromtimestamp(best_tick["time_msc"] / 1000, tz=timezone.utc)
print(f"BEST (most favorable) real tick: bid={float(best_tick['bid']):.2f} at {best_time.isoformat()} true UTC "
      f"-> favorable=${best_favorable:.2f}")

if crossed_at is not None:
    cross_time = datetime.fromtimestamp(crossed_at["time_msc"] / 1000, tz=timezone.utc)
    print(f"\n*** REAL TICKS DID CROSS THE ${args.trigger} TRIGGER at {cross_time.isoformat()} true UTC "
          f"(bid={float(crossed_at['bid']):.2f}) -- breakeven SHOULD have armed. If it didn't, this is a real bug worth investigating. ***")
else:
    print(f"\nReal ticks NEVER reached the ${args.trigger} trigger -- genuine near-miss, "
          f"short by ${args.trigger - best_favorable:.2f}. The 1-minute candle's OHLC low/high overstated "
          f"the real favorable move (candle bars and the live tick stream aren't always identical to the cent). "
          f"No bug -- breakeven correctly did not arm.")
