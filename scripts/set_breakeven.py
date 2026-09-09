"""Turn an account's breakeven stop on, off, or to a different level.

User, 2026-09-09: demo2_m5's breakeven should be OFF. It armed at $9.00,
which an against-trend trade targeting $8.00 can never reach, so it only
ever applied to half the trades -- and demo2_m3, the other leg of the
same account, has none either.

REFUSES to leave a gap when the TP-runner is on. The runner removes the
broker take-profit at the arm point and only locks at the lock level; in
between, the breakeven stop is the ONLY protection. On 2026-09-09 17:48
demo1_m3 had breakeven at +$0.50 in that window, gave back a trade that
had reached +$5.94, and finished at +$1.56 against the control's $71.28.
So with a runner active this will not accept a breakeven above the arm
point, and warns loudly if it is set far below it.

    python scripts/set_breakeven.py --account demo2_m5 --off
    python scripts/set_breakeven.py --account demo1_m5 --trigger 9 --lock 0.71
    ... --apply
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, type=validate_account_name)
    p.add_argument("--off", action="store_true", help="remove the breakeven stop entirely")
    p.add_argument("--trigger", type=float, help="floating profit that arms it")
    p.add_argument("--lock", type=float, help="profit the stop then protects")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.off and args.trigger is None:
        sys.exit("Give --off, or --trigger (and usually --lock).")
    path = PROJECT_ROOT / "config" / f"settings.{args.account}.yaml"
    if not path.exists():
        sys.exit(f"{path.name} not found. Run this on the trading server.")

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    trigger = None if args.off else args.trigger
    lock = None if args.off else args.lock

    tp = float(doc.get("take_profit_usd") or 0)
    trail = doc.get("tp_runner_trail_usd")
    arm_before = float(doc.get("tp_runner_arm_before_usd") or 0)
    arm_point = tp - arm_before

    print("=" * 78)
    print(f"{args.account}  —  breakeven stop")
    print("=" * 78)
    print(f"  breakeven_trigger_usd    {str(doc.get('breakeven_trigger_usd')):>10}  ->  {trigger}")
    print(f"  breakeven_lock_usd       {str(doc.get('breakeven_lock_usd')):>10}  ->  {lock}")

    if trail is not None:
        print(f"\n  This account's TP-runner IS on: it removes the take-profit at "
              f"+${arm_point:.2f}.")
        if trigger is None:
            print(f"  With no breakeven, a trade that arms at +${arm_point:.2f} and turns back")
            print(f"  before the lock has NOTHING protecting it but the stop.")
            sys.exit("REFUSING: do not remove the breakeven while the runner is on.")
        if trigger > arm_point:
            sys.exit(f"REFUSING: breakeven ${trigger:.2f} is above the runner's arm point "
                     f"${arm_point:.2f}. A winner could arm, turn, and still run to a full loss.")
    elif trigger is None:
        stop = doc.get("stop_loss_usd")
        print(f"\n  Breakeven OFF. A trade in profit that turns around now runs to "
              f"{'its stop' if stop else 'the opposite cross, with NO floor'}.")
        print(f"  That matches demo2_m3, which has never had one.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    doc["breakeven_trigger_usd"] = trigger
    doc["breakeven_lock_usd"] = lock
    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                    encoding="utf-8")
    yaml.safe_load(path.read_text(encoding="utf-8"))
    print(f"\n  written and parsed OK: {path.name}")
    print(f"  Restart to load it:  python scripts/restart_bot.py --accounts {args.account} "
          f"--wait-for-flat 120 --start")


if __name__ == "__main__":
    main()
