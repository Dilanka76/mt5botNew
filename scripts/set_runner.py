"""Set the TP-runner's lock and trail, and prove the whole arrangement holds.

The runner has three levels that only make sense together, and getting
any pair right while ignoring the third has now cost money twice:

    arm point   = take_profit - tp_runner_arm_before_usd
                  where the broker take-profit is REMOVED
    lock level  = take_profit - tp_runner_lock_below_usd
                  where the stop jumps up and starts trailing
    breakeven   = entry + breakeven_lock_usd
                  the ONLY protection between those two

WHAT WENT WRONG, TWICE, ON demo1_m3:

  Lock BELOW the target (lock_below $1.00, lock at $5.00). The stop is
  placed at $5.00 while price is at $5.00-$5.20, so any small pullback
  ends the trade immediately. On 2026-09-10 three of four trades exited
  at exactly +$5.00 where the control banked $6.00, and one was shaken
  out at +$4.53 before price ran on to +$8.00.

  Lock AT the target with a loose breakeven. Then nothing guards the
  window between arm and target: on 2026-09-09 17:48 a trade reached
  +$5.94, missed the $6.00 lock by six cents, fell to the +$0.50
  breakeven and paid $1.56 against the control's $71.28.

Both are avoidable at once: lock AT the target so the runner can never
finish below the plain exit, and raise the breakeven so the arm-to-target
window is worth something. This refuses to write a combination that
leaves either hole open.

    python scripts/set_runner.py --account demo1_m3 --lock-below 0 --breakeven-lock 4.5
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
    p.add_argument("--lock-below", type=float, required=True,
                   help="dollars BELOW the target where the stop locks (0 = at the target)")
    p.add_argument("--trail", type=float, default=None, help="leave unset to keep the current trail")
    p.add_argument("--breakeven-lock", type=float, default=None,
                   help="profit the breakeven stop protects — this is what guards the "
                        "window between arming and locking")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = PROJECT_ROOT / "config" / f"settings.{args.account}.yaml"
    if not path.exists():
        sys.exit(f"{path.name} not found. Run this on the trading server.")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))

    tp = float(doc["take_profit_usd"])
    arm_before = float(doc.get("tp_runner_arm_before_usd") or 0)
    arm_point = tp - arm_before
    lock_level = tp - args.lock_below
    trail = args.trail if args.trail is not None else doc.get("tp_runner_trail_usd")
    be_trigger = doc.get("breakeven_trigger_usd")
    be_lock = args.breakeven_lock if args.breakeven_lock is not None else doc.get("breakeven_lock_usd")

    print("=" * 82)
    print(f"{args.account}  —  TP-runner")
    print("=" * 82)
    print(f"  target            ${tp:.2f}")
    print(f"  arm point         ${arm_point:.2f}   take-profit removed here")
    print(f"  lock level        ${lock_level:.2f}   stop jumps here and starts trailing")
    print(f"  trail             ${float(trail):.2f}" if trail else "  trail             (runner off)")
    print(f"  breakeven         arms ${float(be_trigger):.2f} -> keeps ${float(be_lock or 0):.2f}")

    if lock_level > tp + 1e-9:
        sys.exit("REFUSING: the lock cannot sit ABOVE the target.")
    if lock_level <= 0:
        sys.exit("REFUSING: the lock must protect a profit.")

    # Hole 1: lock below the target means the runner can finish worse than
    # simply taking the target.
    if args.lock_below > 0:
        print(f"\n  WARNING a lock ${args.lock_below:.2f} below the target means the runner CAN")
        print(f"          finish worse than just taking ${tp:.2f}. On demo1_m3 that lost")
        print(f"          seven of nine paired trades. Prefer --lock-below 0.")

    # Hole 2: the window between arming and locking, guarded only by breakeven.
    gap = lock_level - arm_point
    if gap > 1e-9:
        worst = float(be_lock or 0)
        print(f"\n  Between +${arm_point:.2f} and +${lock_level:.2f} the take-profit is gone and the")
        print(f"  lock has not engaged. The ONLY protection there is the breakeven, at")
        print(f"  +${worst:.2f}. A trade that peaks inside that window pays ${worst:.2f}, not ${tp:.2f}.")
        if worst < arm_point - (float(trail) if trail else 0.5) - 1e-9:
            sys.exit(
                f"REFUSING: breakeven keeps only ${worst:.2f} in a ${gap:.2f}-wide unguarded "
                f"window.\n  Set --breakeven-lock to about "
                f"${arm_point - (float(trail) if trail else 0.5):.2f} "
                f"(the arm point less one trail), or this repeats 2026-09-09 17:48.")
        print(f"  Covered: a trade shaken out in that window keeps ${worst:.2f}.")

    if be_trigger is not None and float(be_trigger) > arm_point + 1e-9:
        sys.exit(f"REFUSING: breakeven arms at ${float(be_trigger):.2f}, above the ${arm_point:.2f} "
                 f"arm point, so it cannot guard the window at all.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    doc["tp_runner_lock_below_usd"] = args.lock_below
    if args.trail is not None:
        doc["tp_runner_trail_usd"] = args.trail
    if args.breakeven_lock is not None:
        doc["breakeven_lock_usd"] = args.breakeven_lock
    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), encoding="utf-8")
    yaml.safe_load(path.read_text(encoding="utf-8"))
    print(f"\n  written and parsed OK: {path.name}")
    print(f"  Restart to load it:  python scripts/restart_bot.py --accounts {args.account} "
          f"--wait-for-flat 120 --start")


if __name__ == "__main__":
    main()
