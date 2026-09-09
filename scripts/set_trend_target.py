"""Give an account the higher-timeframe trend target.

The rule: on entry, if the trade runs WITH the EMA13/21 trend on a higher
timeframe, aim for the bigger target; if against it, keep the normal one.
It never blocks a trade, and the target never changes once the trade is
open.

  demo2_m3 (2026-09-09): $6.00 normally, $8.00 with the M15 trend.
                         Also runs with NO stop loss (--no-stop).
  demo2_m5 (2026-09-09): $8.00 normally, $10.00 with the M15 trend.
                         Keeps its $10 stop. Note this LOWERS the target
                         for against-trend trades, which previously took
                         $10 flat.

    python scripts/set_trend_target.py --account demo2_m5 --base-tp 8 --trend-tp 10
    python scripts/set_trend_target.py --account demo2_m5 --base-tp 8 --trend-tp 10 --apply

Edits the parsed YAML and re-serialises, never text substitution -- the
text approach wrote an unloadable config on 2026-09-09 by appending a
nested key at top level. Prints before/after for every field it touches
and re-reads the file to prove it still loads.
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
    p.add_argument("--base-tp", type=float, required=True,
                   help="target when the trade runs AGAINST the higher-timeframe trend")
    p.add_argument("--trend-tp", type=float, required=True,
                   help="target when it runs WITH the trend (must be larger)")
    p.add_argument("--timeframe", default="M15", help="the higher timeframe (default M15)")
    p.add_argument("--no-stop", action="store_true",
                   help="ALSO remove the stop loss entirely: a losing trade then runs "
                        "until the opposite cross, with no floor")
    p.add_argument("--apply", action="store_true", help="write the file (default: dry run)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = PROJECT_ROOT / "config" / f"settings.{args.account}.yaml"
    if not path.exists():
        sys.exit(f"{path.name} not found. Run this on the trading server.")
    if args.trend_tp <= args.base_tp:
        sys.exit(f"REFUSING: the trend target (${args.trend_tp:.2f}) must be LARGER than the "
                 f"normal one (${args.base_tp:.2f}). The rule exists to let a trade that "
                 f"agrees with the bigger picture run further.")

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    changes = {
        "take_profit_usd": args.base_tp,
        "htf_trend_timeframe": args.timeframe,
        "htf_trend_take_profit_usd": args.trend_tp,
    }
    if args.no_stop:
        changes["stop_loss_usd"] = None

    print("=" * 78)
    print(f"{args.account}  —  {args.timeframe}-trend target")
    print("=" * 78)
    for key, value in changes.items():
        print(f"  {key:<28} {str(doc.get(key, '(not set)')):>12}  ->  {value}")

    print(f"\n  WITH the {args.timeframe} trend  -> ${args.trend_tp:.2f}")
    print(f"  AGAINST it                -> ${args.base_tp:.2f}")
    print(f"  Decided once at entry from the last CLOSED {args.timeframe} candle.")
    print(f"  It never blocks a trade; only the target moves.")

    # The single breakeven trigger now has to serve two different targets.
    be = doc.get("breakeven_trigger_usd")
    if be is not None and float(be) >= args.base_tp:
        print(f"\n  NOTE breakeven_trigger_usd is ${float(be):.2f}, which is at or above the")
        print(f"       ${args.base_tp:.2f} target. An against-trend trade closes at its target")
        print(f"       BEFORE breakeven could arm, so those trades run on the stop alone.")
        print(f"       Deliberate here: lowering it would fire breakeven far below the")
        print(f"       ${args.trend_tp:.2f} target too, and early breakevens have measured as")
        print(f"       costing money on this project.")
    if args.no_stop:
        print(f"\n  WITH NO STOP LOSS a losing trade has no floor until the opposite cross,")
        print(f"  and a weekend gap can open past anything you would have accepted.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    doc.update(changes)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                    encoding="utf-8")
    yaml.safe_load(path.read_text(encoding="utf-8"))        # prove it still loads
    print(f"\n  written and parsed OK: {path}")
    print(f"\n  Restart it for this to take effect, and only once it is flat:")
    print(f"    python scripts/restart_bot.py --accounts {args.account} --wait-for-flat 120 --start")


if __name__ == "__main__":
    main()
