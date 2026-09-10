"""Find a trade by its price, across every account, and say WHO opened it.

User, 2026-09-10: *"4421.93 this is the entry price, why that trade
taken?"* -- and that price appears nowhere in any bot's decision log.

A decision log only records what a BOT did. A position opened by hand, or
on an account with no bot running, leaves nothing there at all. So this
asks MT5 itself instead, over every account it is given, and reports the
MAGIC NUMBER of whatever it finds. The magic answers the question
outright:

    910003 / 910005 / 920003 / 920005   a bot opened it -- and the
                                        decision log will say why
    0                                   opened by hand. There is no
                                        "why": no rule produced it.
    anything else                       another bot or platform on the
                                        same account

Reading only the logs cannot distinguish "no rule fired" from "a rule
fired on an account we did not look at", and those need very different
answers.

    python scripts/find_trade.py --price 4421.93
    python scripts/find_trade.py --price 4421.93 --days 5 --tolerance 0.30

Read-only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--accounts", default=None,
                   help="default: every account with a config file, live included")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--tolerance", type=float, default=0.15,
                   help="price window either side (default $0.15)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.accounts:
        accounts = [validate_account_name(a) for a in args.accounts.split(",")]
    else:
        accounts = sorted(
            f.name[len("settings."):-len(".yaml")]
            for f in (PROJECT_ROOT / "config").glob("settings.*.yaml")
            if not f.name.endswith(".example.yaml") and f.name != "settings.yaml"
        )
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)

    print("=" * 96)
    print(f"Looking for a position opened near {args.price:.2f} "
          f"(+/- ${args.tolerance:.2f}), last {args.days} days")
    print(f"across: {', '.join(accounts)}")
    print("=" * 96)

    # An account with no .env cannot be queried at all -- say so rather
    # than reporting "not found", which would read as "it does not exist".
    seen, skipped = 0, []
    for account in accounts:
        if not (PROJECT_ROOT / f".env.{account}").exists():
            skipped.append(f"{account} (no credentials)")
            continue
        try:
            config = load_config(account)
            connector = MT5Connector(config.mt5)
            connector.connect()
        except Exception as exc:                       # noqa: BLE001
            skipped.append(f"{account} ({type(exc).__name__})")
            continue
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            deals = mt5.history_deals_get(since + offset, now + offset) or []
            hits = [d for d in deals
                    if d.symbol == config.symbol
                    and abs(d.price - args.price) <= args.tolerance]
            for d in hits:
                when = datetime.fromtimestamp(d.time, tz=timezone.utc) - offset
                who = ("opened BY HAND — no rule produced it" if d.magic == 0
                       else f"magic {d.magic}"
                       + (" — THIS bot" if d.magic == config.execution.magic_number
                          else " — a sibling leg on the same login"))
                print(f"\n  {account}: deal {d.ticket}  position {d.position_id}")
                print(f"    {when:%Y-%m-%d %H:%M:%S} UTC "
                      f"({when.astimezone(COLOMBO):%d %b %H:%M:%S} Colombo)")
                print(f"    {'BUY' if d.type == 0 else 'SELL'} {d.volume} lots at {d.price:.2f}"
                      f"   entry/exit={d.entry}   profit {d.profit:+.2f}")
                print(f"    {who}")
                seen += 1
        finally:
            connector.disconnect()

    if skipped:
        print(f"\n  NOT SEARCHED: {', '.join(skipped)}")
        print("  An account without credentials cannot be queried — that is not the same")
        print("  as the trade not existing there.")
    if not seen:
        print(f"\n  Nothing within ${args.tolerance:.2f} of {args.price:.2f} on any account")
        print(f"  searched, in {args.days} days. Widen with --tolerance or --days, or the")
        print(f"  trade is on a terminal this server does not have credentials for.")


if __name__ == "__main__":
    main()
