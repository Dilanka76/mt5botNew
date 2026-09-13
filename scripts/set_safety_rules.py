"""Set the two live-money safety rules: weekend flat, and a broker backstop.

User's decisions 2026-09-13 for live2:
  - close any position still open when the week's trading ends
  - do NOT put the strategy's stop at the broker (stop-hunting), but DO
    place a much wider one as a disaster brake: $30 on M3, $35 on M5

Neither is a trading rule. The software stop still governs every ordinary
exit, and the weekend close only ever fires on a Friday. Both are
insurance against the bot or the machine failing, which demo never has to
worry about.

REFUSES a backstop that is not clearly wider than the software stop. Set
at or near it, the backstop would start firing on ordinary losing trades
-- changing the strategy instead of insuring it -- and would sit exactly
where a hunt would look, which is the thing it was chosen to avoid.

    python scripts/set_safety_rules.py --account live2_m3 --backstop 30 --weekend-flat 20:00
    python scripts/set_safety_rules.py --account live2_m5 --backstop 35 --weekend-flat 20:00 --apply
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, load_config, validate_account_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, type=validate_account_name)
    p.add_argument("--backstop", type=float, default=None,
                   help="broker-side stop distance, well wider than the software stop")
    p.add_argument("--weekend-flat", default=None, metavar="HH:MM",
                   help='UTC time on FRIDAY to close everything, e.g. "20:00"')
    p.add_argument("--off", action="store_true", help="remove both rules")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = PROJECT_ROOT / "config" / f"settings.{args.account}.yaml"
    if not path.exists():
        sys.exit(f"{path.name} not found. Run this on the trading server.")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    soft = doc.get("stop_loss_usd")
    tiers = doc.get("position_sizing") or [{"max_balance": None, "lots": 0.01}]

    backstop = None if args.off else args.backstop
    flat = None if args.off else args.weekend_flat

    print("=" * 78)
    print(f"{args.account}   safety rules")
    print("=" * 78)
    print(f"  broker_backstop_usd   {str(doc.get('broker_backstop_usd')):>8}  ->  {backstop}")
    print(f"  weekend_flat_utc      {str(doc.get('weekend_flat_utc')):>8}  ->  {flat}")

    if backstop is not None:
        if soft is None:
            sys.exit("\nREFUSING: this account has no software stop, so a backstop would "
                     "become the only stop and change the strategy.")
        if backstop < float(soft) * 2:
            sys.exit(f"\nREFUSING: a ${backstop:.2f} backstop is not clearly wider than the "
                     f"${float(soft):.2f} software stop.\n"
                     f"  It would fire on ordinary losing trades and sit where a hunt would\n"
                     f"  look. Use at least ${float(soft) * 2:.2f}, or --off.")
        print(f"\n  software stop ${float(soft):.2f} governs every normal exit.")
        print(f"  the ${backstop:.2f} backstop only fires if the bot or the machine dies.")
        # Shown across the WHOLE ladder, not just its top tier. The first
        # version printed only the top rung -- "$360 if the bot dies" on an
        # account funded with $300, which reads as though the backstop can
        # lose more than the account. It can, but only at a balance big
        # enough to trade that size.
        print(f"\n    {'balance up to':>16}{'lots':>7}{'normal loss':>13}{'if bot dies':>13}")
        for t in tiers:
            cap = t.get("max_balance")
            lot = float(t["lots"])
            print(f"    {('any' if cap is None else f'${cap:,.0f}'):>16}{lot:>7}"
                  f"{float(soft) * lot * 100:>13.2f}{backstop * lot * 100:>13.2f}")
        print(f"\n    Your lot size comes from the BALANCE, so read the row your account")
        print(f"    is in. The backstop scales with it -- it is a cap, not a fixed amount.")

    if flat:
        print(f"\n  every Friday from {flat} UTC: no new entries, and any open position")
        print(f"  is closed -- profit or loss. It cannot fire on any other day.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    doc["broker_backstop_usd"] = backstop
    doc["weekend_flat_utc"] = flat
    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                    encoding="utf-8")
    load_config(args.account)
    print(f"\n  written and loaded OK: {path.name}")
    print(f"  Takes effect on the next restart:")
    print(f"    python scripts/restart_bot.py --accounts {args.account} --wait-for-flat 120 --start")


if __name__ == "__main__":
    main()
