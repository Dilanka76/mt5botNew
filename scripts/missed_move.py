"""How far did price keep going AFTER each trade closed?

User, 2026-09-09, about the 10:03 BUY that stopped out at +$5.65 while
price carried on: *"after this buy and till now can you look more trend
gain, roughly $20 we missed it, i think this is the reason why i am told
you before"*.

Measures exactly that: from each exit price, the furthest price travelled
in the trade's OWN direction over several horizons -- the money left on
the table -- and, in the same breath, how far it went the OTHER way,
which is what holding would actually have cost when it did not run.

Reporting only the favourable side is how "we should have held on" gets
believed. Gold moves several dollars in any half hour; the question is
never whether it moved but whether it moved MORE in your direction than
against it, systematically. Both columns, always.

Post-exit movement was measured once before (project_post_exit_movement_null,
2026-09-05) and came back a random walk -- favourable and adverse
continuation both scaling as sqrt(time), which killed the case for wider
take-profits and trailing stops. That study covered a different window, so
this re-measures on current trades and prints the aggregate underneath the
per-trade rows: one vivid example is not evidence, and this script exists
to keep both in view at once.

M1 candles are used for the post-exit window whatever the account's own
timeframe, since a 3- or 5-minute candle is too coarse to see where price
actually turned.

    python scripts/missed_move.py --accounts demo1_m3,demo1_m5 --since "2026-09-09 02:00:00"

Read-only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m3,demo1_m5")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--horizons", default="15,30,60", help="minutes after the exit to look")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    horizons = [int(h) for h in args.horizons.split(",")]

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        c = load_config(account)
        connector = MT5Connector(c.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, c.symbol)
            trades = get_closed_trades_range(c.symbol, c.execution.magic_number, since, now, offset)
            m1 = get_ohlc_range(connector, c.symbol, "M1", since - timedelta(hours=1),
                                now + timedelta(minutes=5))
        finally:
            connector.disconnect()
        trades.sort(key=lambda t: t["entry_time"])

        print("=" * 96)
        print(f"{account} ({c.timeframe})  TP ${c.take_profit_usd:.2f} — what happened AFTER each exit")
        print("=" * 96)
        if not trades:
            print("  No closed trades in this window.\n")
            continue

        head = "".join(f"{f'+{h}m':>18}" for h in horizons)
        print(f"  {'exit time':<10}{'dir':>5}{'exit px':>10}{head}")
        print(f"  {'':<10}{'':>5}{'':>10}" + "".join(f"{'ran / against':>18}" for _ in horizons))

        totals = {h: {"fav": [], "adv": []} for h in horizons}
        for t in trades:
            exit_t = t["exit_time"].astimezone(timezone.utc)
            exit_px = float(t["exit_price"])
            is_buy = t["direction"] == "BUY"
            cells = ""
            for h in horizons:
                window = m1[(m1.index > exit_t) & (m1.index <= exit_t + timedelta(minutes=h))]
                if window.empty:
                    cells += f"{'—':>18}"
                    continue
                hi, lo = float(window["high"].max()), float(window["low"].min())
                fav = (hi - exit_px) if is_buy else (exit_px - lo)
                adv = (exit_px - lo) if is_buy else (hi - exit_px)
                totals[h]["fav"].append(fav)
                totals[h]["adv"].append(adv)
                cells += f"{f'+{fav:.2f} / -{adv:.2f}':>18}"
            print(f"  {exit_t.astimezone(COLOMBO):%H:%M:%S}{t['direction']:>5}{exit_px:>10.2f}{cells}")

        print(f"\n  AVERAGE over {len(trades)} trades — the number that matters")
        print(f"    {'horizon':<10}{'ran on':>12}{'went against':>14}{'net':>10}   verdict")
        for h in horizons:
            fav, adv = totals[h]["fav"], totals[h]["adv"]
            if not fav:
                continue
            f, a = sum(fav) / len(fav), sum(adv) / len(adv)
            print(f"    +{h:<9}m{f:>12.2f}{a:>14.2f}{f - a:>10.2f}   "
                  f"{'more room in our direction' if f - a > 0.5 else 'roughly symmetric — a coin flip' if abs(f - a) <= 0.5 else 'we exited at a good moment'}")

        print()
        print("  Read the AVERAGE, not the best row. Gold moves several dollars in any half")
        print("  hour, so there is ALWAYS a trade that looks like it should have been held.")
        print("  Holding only pays if 'ran on' beats 'went against' consistently -- and when")
        print("  this was measured across a larger sample it came out symmetric, which is")
        print("  why wider take-profits and trailing stops were ruled out before.")


if __name__ == "__main__":
    main()
