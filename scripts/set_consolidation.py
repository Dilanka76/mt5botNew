"""Switch the consolidation filter on for an account, and choose whether it
RECORDS or actually SKIPS.

User, 2026-09-20: he cannot see sideways consolidation by eye and wants
the bot to leave those crosses alone -- and wants to watch it happen on
demo2 rather than read about it.

Three states per account, and the middle one is the useful one:

  --off            no block in the file. The engine behaves as it always did.
  --record         enabled, shadow_only: true. Writes cons_overlap and
                   cons_box_atr on EVERY trade_entered line and enters
                   anyway. Not one trade changes. This is what live2 runs.
  --skip           enabled, shadow_only: false. Really withholds the entry
                   and logs entry_skipped_consolidation. This is what demo2
                   runs once the thresholds are known.

Start every account on --record. The raw numbers land in the decision log
from the first trade, so ANY threshold can be scored afterwards from real
forward data instead of chosen now and defended later.

    python scripts/set_consolidation.py --accounts demo2_m3,demo2_m5,live2_m3,live2_m5 --record
    python scripts/set_consolidation.py --accounts demo2_m3,demo2_m5 --skip --overlap-min 0.62 --box-atr-max 1.8 --apply

Prints the exact before/after and changes nothing without --apply.
A bot reads its config ONCE at startup, so this takes effect on the next
restart of that account -- and never restart a bot holding a position.
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name
from bot.timeframes import TIMEFRAME_MINUTES

DEFAULTS = {"enabled": True, "timeframe": "M15", "lookback": 6,
            "overlap_min": 0.60, "box_atr_max": 2.0, "shadow_only": True}

KEY = "consolidation_filter"


def render(block: dict) -> str:
    """The YAML text for the block, with the comments a human needs when
    they open this file in six months."""
    mode = ("RECORDS ONLY -- every entry is still taken" if block["shadow_only"]
            else "SKIPS entries while price is boxed in")
    return (
        f"\n{KEY}:\n"
        f"  # {mode}.\n"
        f"  # A box = higher-timeframe candles covering the same prices AND a\n"
        f"  # small range. Both conditions, or it is ordinary market movement.\n"
        f"  # Entries only: a position still closes on the opposite cross.\n"
        f"  enabled: {str(block['enabled']).lower()}\n"
        f"  timeframe: {block['timeframe']}        # the zoom-out chart\n"
        f"  lookback: {block['lookback']}                # how many of those candles\n"
        f"  overlap_min: {block['overlap_min']}       # candles covering the same prices\n"
        f"  box_atr_max: {block['box_atr_max']}       # range height, in ATRs\n"
        f"  shadow_only: {str(block['shadow_only']).lower()}      "
        f"# true = measure and log, change no trade\n"
    )


def splice(text: str, block: dict | None) -> str:
    """Replace (or remove, or append) the block WITHOUT rewriting the file.

    yaml.safe_dump would be shorter and is what set_symbol.py and
    set_daily_loss.py do -- and it silently deletes every comment in the
    file. settings.demo2_m3.yaml carries eight, including "CONFIRM against
    the real demo2 account before going live". Those are worth more than
    the few lines this costs.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i, found = 0, False
    while i < len(lines):
        if lines[i].startswith(f"{KEY}:"):
            found = True
            i += 1
            while i < len(lines):
                if lines[i].startswith((" ", "\t")):
                    i += 1
                    continue
                if not lines[i].strip():        # a blank line inside the block
                    nxt = next((j for j in range(i + 1, len(lines)) if lines[j].strip()), None)
                    if nxt is not None and lines[nxt].startswith((" ", "\t")):
                        i += 1
                        continue
                break
            if block is not None:
                out.append(render(block).lstrip("\n"))
            continue
        out.append(lines[i])
        i += 1
    result = "".join(out)
    if block is not None and not found:
        if not result.endswith("\n"):
            result += "\n"
        result += render(block)
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="measure only, change no trade")
    mode.add_argument("--skip", action="store_true", help="really withhold entries in a box")
    mode.add_argument("--off", action="store_true", help="remove the block entirely")
    p.add_argument("--timeframe", default=None, help="the zoom-out chart (default M15)")
    p.add_argument("--lookback", type=int, default=None, help="how many HTF candles (default 6)")
    p.add_argument("--overlap-min", type=float, default=None)
    p.add_argument("--box-atr-max", type=float, default=None)
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a.strip()) for a in args.accounts.split(",")]

    for key, value in (("timeframe", args.timeframe), ("lookback", args.lookback)):
        if value is not None and key == "timeframe" and value not in TIMEFRAME_MINUTES:
            raise SystemExit(f"--timeframe {value!r} is not a timeframe this bot can fetch.")
    if args.lookback is not None and args.lookback < 2:
        raise SystemExit("--lookback must be at least 2.")
    for name, v in (("--overlap-min", args.overlap_min), ("--box-atr-max", args.box_atr_max)):
        if v is not None and v <= 0:
            raise SystemExit(f"{name} must be above zero.")
    if args.overlap_min is not None and args.overlap_min > 1:
        raise SystemExit("--overlap-min is a share of the candle range, so it cannot exceed 1.0.")

    for account in accounts:
        path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        if not path.is_file():
            print(f"\n{account}: no settings file at {path} -- skipped.")
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        before = raw.get("consolidation_filter")

        if args.off:
            after = None
        else:
            after = dict(DEFAULTS)
            if isinstance(before, dict):
                after.update(before)          # keep thresholds already chosen
            after["shadow_only"] = bool(args.record)
            after["enabled"] = True
            for key, value in (("timeframe", args.timeframe), ("lookback", args.lookback),
                               ("overlap_min", args.overlap_min),
                               ("box_atr_max", args.box_atr_max)):
                if value is not None:
                    after[key] = value

        print(f"\n{account}  ({path.name})")
        print(f"  before: {before if before is not None else 'no block -- filter absent'}")
        print(f"  after : {after if after is not None else 'no block -- filter absent'}")
        if after is not None:
            if after["shadow_only"]:
                print("  -> RECORDS only. Every entry is still taken; not one trade changes.")
            else:
                print(f"  -> SKIPS entries when {after['timeframe']} candles overlap "
                      f">= {after['overlap_min']} AND the range is <= {after['box_atr_max']} ATR.")
                print("     Exits are never blocked: a position still closes on the opposite cross.")
        if before == after:
            print("  (already exactly this -- nothing to write)")
            continue

        if not args.apply:
            print("  DRY RUN -- re-run with --apply to write it")
            continue

        original = path.read_text(encoding="utf-8")
        updated = splice(original, after)
        # Never write something that cannot be read back: parse the result
        # and check every OTHER setting survived untouched.
        reparsed = yaml.safe_load(updated)
        untouched = {k: v for k, v in raw.items() if k != KEY}
        if {k: v for k, v in reparsed.items() if k != KEY} != untouched:
            raise SystemExit(f"{account}: the edit would have changed another setting. "
                             f"Nothing written.")
        if reparsed.get(KEY) != after:
            raise SystemExit(f"{account}: the block did not read back as intended. "
                             f"Nothing written.")
        path.write_text(updated, encoding="utf-8")
        kept = original.count("#")
        print(f"  WRITTEN  (all {kept} existing comment line(s) preserved)")

    print("\nA bot reads its config once, at startup, so restart each account for this to")
    print("take effect -- and never restart one that is holding an open position.")
    print("Confirm afterwards with:  python scripts/config_is_live.py --accounts " + args.accounts)


if __name__ == "__main__":
    main()
