"""Create the demo1_m5 leg from fitted values, and retire demo1_m1.

User's decision 2026-09-09: *"the demo1 instead of the 1m setup the 5m"*.
demo1 keeps running two bots -- M5 + M3 instead of M1 + M3. demo1_m3 and
both demo2 legs are not touched, so the controls survive.

WHAT THIS DOES NOT DO: choose any number. Every level must come from
scripts/fit_new_timeframe.py and be passed in explicitly. There is no
default for the stop or the take-profit, on purpose -- a default here
would eventually get deployed by someone in a hurry, which is exactly
how the colour+volume filter went live and cost $192 before being
retracted.

REFUSES TO RUN unless the values are mutually consistent:

  stop >= 2x the measured median M5 candle range ($4.68, so >= $9.36).
    A stop inside one candle's ordinary range is hit by noise before the
    signal can work. This is the floor that killed the idea of simply
    carrying M3's $7 across.

  breakeven == take_profit - arm_before, exactly. On 2026-09-08 M3 ran
    with breakeven $5.50 while its take-profit was removed at $5.00, so
    a +$5 winner could still fall to -$7.

  arm_before == 1.00. It is a race against the broker's take-profit fill,
    decided by dollars travelled per SECOND, which is the same market on
    every chart. demo1_m1's $0.20 lost that race 7 times in 10.

  top lot size x stop x 100 within 10% of demo1_m3's risk per trade.
    A bigger stop at unchanged lots would raise real risk silently.

The MT5 login is REUSED from demo1_m1 (demo1_m1 and demo1_m3 already
share one demo1 login, the same way demo2 does), so .env.demo1_m1 is
copied byte-for-byte with shutil -- never read, never printed, never
logged.

    python scripts/create_m5_leg.py --stop 9.5 --take-profit 8.0 \
        --trail 0.75 --lock-below 1.0            # dry run, writes nothing
    python scripts/create_m5_leg.py ... --apply  # actually writes

Retiring demo1_m1 is a separate, deliberate step (--retire-m1), and it
never kills a bot holding an open position.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT

SOURCE = "demo1_m3"          # rules and structure are copied from here
DONOR = "demo1_m1"           # MT5 credentials come from here
NEW = "demo1_m5"

M5_MEDIAN_RANGE = 4.68       # measured, scripts/timeframe_expectancy.py 2026-09-08
ARM_BEFORE = 1.00            # fixed, not scaled — see the docstring


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stop", type=float, required=True, help="fitted stop_loss_usd")
    p.add_argument("--take-profit", type=float, required=True, help="fitted take_profit_usd")
    p.add_argument("--trail", type=float, default=None,
                   help="fitted tp_runner_trail_usd; omit to ship with the runner OFF")
    p.add_argument("--lock-below", type=float, default=None, help="fitted tp_runner_lock_below_usd")
    p.add_argument("--magic", type=int, default=None, help="default: donor magic + 4")
    p.add_argument("--apply", action="store_true", help="actually write files (default: dry run)")
    p.add_argument("--retire-m1", action="store_true",
                   help="also disable demo1_m1's scheduled tasks and stop it once flat")
    return p.parse_args()


def read_yaml_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].split("#")[0].strip()
    return None


def check(ok: bool, message: str, failures: list[str]) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def main() -> None:
    args = parse_args()
    cfg_dir = PROJECT_ROOT / "config"
    src_cfg = cfg_dir / f"settings.{SOURCE}.yaml"
    donor_env = PROJECT_ROOT / f".env.{DONOR}"
    new_cfg = cfg_dir / f"settings.{NEW}.yaml"
    new_env = PROJECT_ROOT / f".env.{NEW}"

    for path in (src_cfg, donor_env):
        if not path.exists():
            sys.exit(f"Missing {path.name}. Run this on the trading server, not a dev machine.")

    breakeven = round(args.take_profit - ARM_BEFORE, 2)
    m3_stop = float(read_yaml_value(src_cfg, "stop_loss_usd") or 7.0)
    m3_lots = 0.12                                   # top of demo1_m3's sizing ladder
    m3_risk = m3_stop * m3_lots * 100
    lots = round(m3_risk / (args.stop * 100), 2)
    scale = lots / m3_lots

    print("=" * 78)
    print(f"CREATE {NEW}   (rules from {SOURCE}, MT5 login from {DONOR})")
    print("=" * 78)
    failures: list[str] = []
    check(args.stop >= 2 * M5_MEDIAN_RANGE,
          f"stop ${args.stop:.2f} >= 2x M5 median candle range (${2 * M5_MEDIAN_RANGE:.2f})", failures)
    check(args.take_profit > 0 and args.take_profit < args.stop * 2,
          f"take-profit ${args.take_profit:.2f} is sane against the stop", failures)
    check(breakeven > 0, f"breakeven ${breakeven:.2f} = TP - arm_before (derived, not chosen)", failures)
    check(abs(lots * args.stop * 100 - m3_risk) / m3_risk <= 0.10,
          f"risk/trade ${lots * args.stop * 100:.0f} matches {SOURCE}'s ${m3_risk:.0f} "
          f"(lots {m3_lots} -> {lots})", failures)
    if args.trail is not None:
        check(args.lock_below is not None and args.lock_below < args.take_profit,
              f"lock at +${args.take_profit - (args.lock_below or 0):.2f} is below the take-profit", failures)
    else:
        print("  NOTE  runner OFF — and NOT because it was tested and failed.")
        print("        bot/backtest/runner.py does not simulate tp_runner at all, so a")
        print("        timeframe that has never traded cannot have one fitted. Turn it on")
        print("        later from real exits via scripts/simulate_tp_runner.py.")

    if failures:
        print(f"\nREFUSING: {len(failures)} check(s) failed. Fix the values, do not override.")
        sys.exit(1)

    donor_magic = int(read_yaml_value(cfg_dir / f"settings.{DONOR}.yaml", "magic_number") or 900001)
    magic = args.magic or donor_magic + 4
    src_magic = int(read_yaml_value(src_cfg, "magic_number") or 900003)

    text = src_cfg.read_text(encoding="utf-8")
    header = (f"# {NEW} — M5 leg. Same rules as {SOURCE}; levels fitted by\n"
              f"# scripts/fit_new_timeframe.py, NOT scaled by hand. Replaces {DONOR},\n"
              f"# whose M1 signal lost money in every walk-forward cut.\n"
              f"# Secrets live in .env.{NEW}, not here.\n")
    text = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    replacements = {
        "timeframe": "M5",
        "take_profit_usd": f"{args.take_profit:.2f}",
        "stop_loss_usd": f"{args.stop:.2f}",
        "breakeven_trigger_usd": f"{breakeven:.2f}   # = take_profit - arm_before, derived",
        "tp_runner_arm_before_usd": f"{ARM_BEFORE:.2f}   # fixed: a race against the broker fill, not a candle size",
        "tp_runner_trail_usd": "null" if args.trail is None else f"{args.trail:.2f}",
        "tp_runner_lock_below_usd": "null" if args.lock_below is None else f"{args.lock_below:.2f}",
        "swap_immediate": "true",
        "magic_number": f"{magic}",
        "sibling_magic_numbers": f"[{src_magic}, {donor_magic}]   # {SOURCE}, {DONOR} — same demo1 login",
    }
    out, seen = [], set()
    for line in text.splitlines():
        key = line.strip().split(":", 1)[0] if ":" in line else None
        if key in replacements and key not in seen:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}{key}: {replacements[key]}")
            seen.add(key)
        elif line.strip().startswith("- {max_balance"):
            # scale the whole sizing ladder so risk per trade is unchanged
            scaled = line
            for token in line.split("lots: ")[1:]:
                old = float(token.split("}")[0])
                scaled = scaled.replace(f"lots: {old}", f"lots: {max(0.01, round(old * scale, 2))}")
            out.append(scaled)
        else:
            out.append(line)
    for key in replacements:
        if key not in seen:
            out.append(f"{key}: {replacements[key]}")
    body = header + "\n".join(out).rstrip() + "\n"

    print(f"\n  magic_number  : {magic}   (siblings: {src_magic}, {donor_magic})")
    print(f"  lot ladder    : scaled x{scale:.3f}, top {m3_lots} -> {lots}")
    print(f"  writes        : {new_cfg.name}, .env.{NEW} (copied from .env.{DONOR}, never read)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    if new_cfg.exists() or new_env.exists():
        sys.exit(f"{NEW} already exists. Delete it deliberately before recreating.")
    new_cfg.write_text(body, encoding="utf-8")
    shutil.copyfile(donor_env, new_env)          # bytes only; contents never touched
    print(f"\n  written: {new_cfg}")
    print(f"  written: {new_env}")

    # demo1_m3 must learn about its new sibling, or its duplicate-position
    # guard will treat M5's position as a foreign trade and CLOSE it.
    src_text = src_cfg.read_text(encoding="utf-8")
    if str(magic) not in src_text:
        for line in src_text.splitlines():
            if line.strip().startswith("sibling_magic_numbers:"):
                updated = line.replace("]", f", {magic}]", 1)
                src_cfg.write_text(src_text.replace(line, updated), encoding="utf-8")
                print(f"  updated: {src_cfg.name} sibling_magic_numbers += {magic}")
                break

    if args.retire_m1:
        print(f"\n  retiring {DONOR} (never killed while holding a position):")
        for leg in (f"MT5Bot-{DONOR}",):
            r = subprocess.run(["schtasks", "/Change", "/TN", leg, "/DISABLE"],
                               capture_output=True, text=True)
            print(f"    schtasks disable {leg}: {r.stdout.strip() or r.stderr.strip()}")
        print(f"    now run: python scripts/restart_bot.py --account {DONOR} --wait-for-flat")
        print(f"             (stops it only once it is flat — your PLEASE WAIT rule)")

    print(f"\nNEXT, and none of it is automatic:")
    print(f"  1. python scripts/verify_live2.py --account {NEW}   (or show_strategy.py) — read it back")
    print(f"  2. Task Scheduler GUI: copy the {DONOR} task, point it at {NEW}.")
    print(f"     Windows will ask YOU for the password — I do not handle it.")
    print(f"  3. Leave {SOURCE} and both demo2 legs alone. They are the controls.")


if __name__ == "__main__":
    main()
