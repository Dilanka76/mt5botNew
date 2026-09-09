"""Apply demo2_m3's two changes: no stop loss, and the M15-trend target.

User's rule, 2026-09-09, confirmed explicitly:
  - NO stop loss. A losing trade is held until the opposite cross.
  - On entry, if the trade runs WITH the M15 EMA13/21 trend, aim for
    $8.00; if it runs against it, keep $6.00. The trend never blocks a
    trade and the target never changes once the trade is open.

Edits the parsed YAML and re-serialises, rather than substituting text --
the text approach wrote an unloadable config on 2026-09-09 by appending a
nested key at top level.

    python scripts/set_demo2_m3_trend_target.py              # dry run
    python scripts/set_demo2_m3_trend_target.py --apply

Prints the before/after of every field it touches, because "no stop loss"
is a large enough change that it should be visible, not implied.
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT

ACCOUNT = "demo2_m3"
CHANGES = {
    "stop_loss_usd": None,              # hold to the opposite cross
    "htf_trend_timeframe": "M15",
    "htf_trend_take_profit_usd": 8.0,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write the file (default: dry run)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = PROJECT_ROOT / "config" / f"settings.{ACCOUNT}.yaml"
    if not path.exists():
        sys.exit(f"{path.name} not found. Run this on the trading server.")

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    print("=" * 74)
    print(f"{ACCOUNT}  —  no stop loss, M15-trend take-profit")
    print("=" * 74)
    for key, value in CHANGES.items():
        before = doc.get(key, "(not set)")
        print(f"  {key:<28} {str(before):>12}  ->  {value}")

    tp = float(doc.get("take_profit_usd", 6.0))
    print(f"\n  take_profit_usd stays at ${tp:.2f} — that is the target when the trade")
    print(f"  runs AGAINST the M15 trend. With the trend it becomes "
          f"${CHANGES['htf_trend_take_profit_usd']:.2f}.")
    if CHANGES["htf_trend_take_profit_usd"] <= tp:
        sys.exit("\nREFUSING: the trend target must be LARGER than the normal one.")
    if doc.get("breakeven_trigger_usd") is not None:
        print(f"\n  NOTE breakeven_trigger_usd is {doc['breakeven_trigger_usd']} — with no stop "
              f"loss\n       that becomes the ONLY protective stop, arming only in profit.")

    print(f"\n  WITH NO STOP LOSS a losing trade has no floor until the opposite cross,")
    print(f"  and a weekend gap can open past anything you would have accepted. That is")
    print(f"  the intended behaviour here, not an oversight.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    doc.update(CHANGES)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                    encoding="utf-8")
    yaml.safe_load(path.read_text(encoding="utf-8"))     # prove it still loads
    print(f"\n  written and parsed OK: {path}")
    print(f"\n  Restart demo2_m3 for it to take effect, and only once it is flat:")
    print(f"    python scripts/restart_bot.py --accounts {ACCOUNT} --wait-for-flat 120 --start")


if __name__ == "__main__":
    main()
