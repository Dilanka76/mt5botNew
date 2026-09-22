"""Switch the RANGE filter for an account: record it, really skip, or off.

User, 2026-09-22: skip entries taken inside a trader-drawn range. The
backward test of the frozen definition found range entries worse than the
rest on all six accounts, but too few of them to prove it -- so the plan is
demo2 really SKIPPING (demo money) while live2 only RECORDS, and forward
trades decide whether live2 ever skips.

  --record   enabled, shadow_only: true. Every trade_entered line gets
             in_range / range_ceiling / range_floor; not one trade changes.
  --skip     enabled, shadow_only: false. Entries inside a range are
             withheld and logged as entry_skipped_range. Exits never.
  --off      no block in the file; the engine behaves as it always did.

DELIBERATELY NO TUNING FLAGS. The definition (M15, 16 candles, fractal 2,
0.35 ATR) is frozen: it is what was tested, and changing it after seeing
results would make every result so far meaningless.

    python scripts/set_range_filter.py --accounts live2_m3,live2_m5 --record
    python scripts/set_range_filter.py --accounts demo2_m3,demo2_m5 --skip --apply

Edits the block as TEXT so every comment in the file survives, re-reads
the result and refuses to write if any other setting moved. Takes effect
on the next restart -- never restart a bot holding a position.
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name

KEY = "range_filter"
FROZEN = {"enabled": True, "timeframe": "M15", "lookback": 16, "fractal": 2,
          "level_tolerance_atr": 0.35, "shadow_only": True}


def render(block: dict) -> str:
    mode = ("RECORDS ONLY -- every entry is still taken" if block["shadow_only"]
            else "SKIPS entries inside a range")
    return (
        f"\n{KEY}:\n"
        f"  # {mode}.\n"
        f"  # A range = a ceiling and a floor on the M15, each touched twice in the\n"
        f"  # last 4 hours. Entries only: a position still closes on the opposite cross.\n"
        f"  # FROZEN DEFINITION -- do not tune (see scripts/set_range_filter.py).\n"
        f"  enabled: {str(block['enabled']).lower()}\n"
        f"  timeframe: {block['timeframe']}\n"
        f"  lookback: {block['lookback']}\n"
        f"  fractal: {block['fractal']}\n"
        f"  level_tolerance_atr: {block['level_tolerance_atr']}\n"
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
    mode.add_argument("--skip", action="store_true", help="really withhold entries inside a range")
    mode.add_argument("--off", action="store_true", help="remove the block entirely")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        if not path.is_file():
            print(f"\n{account}: no settings file at {path} -- skipped.")
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        before = raw.get(KEY)
        after = None if args.off else dict(FROZEN, shadow_only=bool(args.record))

        print(f"\n{account}  ({path.name})")
        print(f"  before: {before if before is not None else 'no block -- filter absent'}")
        print(f"  after : {after if after is not None else 'no block -- filter absent'}")
        if after is not None:
            print("  -> RECORDS only. Every entry is still taken." if after["shadow_only"]
                  else "  -> SKIPS entries inside an M15 range. Exits are never blocked.")
        if before == after:
            print("  (already exactly this -- nothing to write)")
            continue
        if not args.apply:
            print("  DRY RUN -- re-run with --apply to write it")
            continue
        original = path.read_text(encoding="utf-8")
        updated = splice(original, after)
        reparsed = yaml.safe_load(updated)
        if {k: v for k, v in reparsed.items() if k != KEY} != {k: v for k, v in raw.items() if k != KEY}:
            raise SystemExit(f"{account}: the edit would have changed another setting. Nothing written.")
        if reparsed.get(KEY) != after:
            raise SystemExit(f"{account}: the block did not read back as intended. Nothing written.")
        path.write_text(updated, encoding="utf-8")
        print(f"  WRITTEN  (all {original.count('#')} existing comment line(s) preserved)")

    print("\nA bot reads its config once, at startup: restart each account for this to take")
    print("effect -- and never restart one that is holding an open position.")


if __name__ == "__main__":
    main()
