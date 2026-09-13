"""Change an account's daily loss limit, nothing else.

User, 2026-09-13: live2 starts on $150/leg and the number will be revised
once real trades exist. clone_strategy can set it, but that rewrites every
strategy field from a source account -- fine on day one when live2 IS a
copy of demo1, wrong later once the two diverge. This touches one field.

The limit blocks NEW entries once the day's REALISED net P/L is down by
this much (Colombo day). It never touches an open position: a trade
already running keeps its stop, its take-profit and its swap exit.

Reports the number against the account's real balance and its stop size,
because "$150" means nothing on its own -- on a $300 account it is half
the money, and on a $3,000 account it is a sensible brake.

    python scripts/set_daily_loss.py --accounts live2_m3,live2_m5 --usd 60
    python scripts/set_daily_loss.py --accounts live2_m3,live2_m5 --usd 60 --apply
    python scripts/set_daily_loss.py --accounts live2_m3 --off --apply

Takes effect on the next restart of that bot.
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    p.add_argument("--usd", type=float, default=None, help="the new limit")
    p.add_argument("--off", action="store_true", help="remove the limit entirely")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.usd is None and not args.off:
        sys.exit("Give --usd <amount>, or --off.")
    new = None if args.off else abs(args.usd)

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        if not path.exists():
            print(f"  {account}: no config, skipped.")
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        old = doc.get("daily_loss_limit_usd")

        print("=" * 74)
        print(f"{account}   daily loss limit   {old}  ->  {new}")
        print("=" * 74)

        # Against the real balance, or the number means nothing.
        try:
            c = load_config(account)
            connector = MT5Connector(c.mt5)
            connector.connect()
            try:
                balance = connector.account_info().balance
            finally:
                connector.disconnect()
            lots = calculate_lots(balance, c.position_sizing)
            risk = (c.stop_loss_usd or 0) * lots * 100
            print(f"  balance ${balance:,.2f}   {lots} lots   ${risk:.0f} risk per trade")
            if new and balance:
                print(f"  the limit is {100 * new / balance:.0f}% of the balance"
                      + (f", about {new / risk:.1f} full stops" if risk else ""))
                if new / balance > 0.33:
                    print(f"  NOTE more than a third of the account. It will not stop a bad day")
                    print(f"       before most of the money has gone.")
            if new is None:
                print(f"  NOTE no limit: a losing day runs until you stop it by hand.")
        except Exception as exc:                        # noqa: BLE001
            print(f"  (could not read the balance: {type(exc).__name__})")

        if not args.apply:
            print("\n  DRY RUN — nothing written.\n")
            continue
        doc["daily_loss_limit_usd"] = new
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                        encoding="utf-8")
        load_config(account)          # prove it still loads
        print(f"\n  written and loaded OK. Takes effect on the next restart:")
        print(f"    python scripts/restart_bot.py --accounts {account} --wait-for-flat 120 --start\n")


if __name__ == "__main__":
    main()
