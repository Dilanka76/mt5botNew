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
import copy
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT

# Defaults reproduce the original demo1 build; --new/--source/--donor
# make the same script build demo2_m5 from demo2's own accounts, so the
# two legs cannot drift apart through hand-editing.
SOURCE = "demo1_m3"          # rules and structure are copied from here
DONOR = "demo1_m1"           # MT5 credentials come from here
NEW = "demo1_m5"

TIMEFRAME = "M5"
M5_MEDIAN_RANGE = 4.68       # measured, scripts/timeframe_expectancy.py 2026-09-08
ARM_BEFORE = 1.00            # fixed, not scaled — see the docstring


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--new", default=NEW, help="account to create (default demo1_m5)")
    p.add_argument("--source", default=SOURCE, help="account whose RULES are copied")
    p.add_argument("--donor", default=DONOR, help="account whose MT5 LOGIN is reused")
    p.add_argument("--stop", type=float, required=True, help="fitted stop_loss_usd")
    p.add_argument("--take-profit", type=float, required=True, help="fitted take_profit_usd")
    p.add_argument("--trail", type=float, default=None,
                   help="fitted tp_runner_trail_usd; omit to ship with the runner OFF")
    p.add_argument("--lock-below", type=float, default=None, help="fitted tp_runner_lock_below_usd")
    p.add_argument("--magic", type=int, default=None, help="default: donor magic + 4")
    p.add_argument("--apply", action="store_true", help="actually write files (default: dry run)")
    p.add_argument("--replace", action="store_true",
                   help="overwrite an existing demo1_m5 config (e.g. one this script "
                        "wrote badly). Never touches an account that is running.")
    p.add_argument("--retire-m1", action="store_true",
                   help="also disable demo1_m1's scheduled tasks and stop it once flat")
    return p.parse_args()


def read_yaml_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].split("#")[0].strip()
    return None


def build_document(doc: dict, *, timeframe: str, stop: float, take_profit: float,
                   breakeven: float, trail: float | None, lock_below: float | None,
                   magic: int, siblings: list[int], scale: float,
                   price_scale: float = 1.0) -> dict:
    """Return the source config edited into the new leg.

    Pure and separately testable on purpose. The first version did string
    substitution on the YAML text and appended any key it had not found to
    the end of the file, which put nested keys at top level and produced a
    config yaml.safe_load could not read at all -- discovered only when the
    file was already written. Editing the parsed document cannot emit a
    structurally invalid file.
    """
    doc = copy.deepcopy(doc)
    doc["timeframe"] = timeframe
    doc["take_profit_usd"] = round(take_profit, 2)
    doc["stop_loss_usd"] = round(stop, 2)
    doc["breakeven_trigger_usd"] = breakeven
    doc["tp_runner_arm_before_usd"] = ARM_BEFORE
    doc["tp_runner_trail_usd"] = trail
    # A required float, unlike the trail: 0.0 is inert while the runner is
    # off and means "lock at the take-profit" once it is on.
    doc["tp_runner_lock_below_usd"] = 0.0 if lock_below is None else lock_below
    doc["swap_immediate"] = True
    # A price level like any other: scale it, or M5 locks M3's $0.50.
    if doc.get("breakeven_lock_usd") is not None:
        doc["breakeven_lock_usd"] = round(float(doc["breakeven_lock_usd"]) * price_scale, 2)
    doc["execution"]["magic_number"] = magic
    doc["execution"]["sibling_magic_numbers"] = list(siblings)
    # Rescale the sizing ladder so risk per trade survives the bigger stop.
    for tier in doc.get("position_sizing", []):
        tier["lots"] = max(0.01, round(float(tier["lots"]) * scale, 2))
    return doc


def check(ok: bool, message: str, failures: list[str]) -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def main() -> None:
    global SOURCE, DONOR, NEW
    args = parse_args()
    SOURCE, DONOR, NEW = args.source, args.donor, args.new
    cfg_dir = PROJECT_ROOT / "config"
    src_cfg = cfg_dir / f"settings.{SOURCE}.yaml"
    donor_env = PROJECT_ROOT / f".env.{DONOR}"
    new_cfg = cfg_dir / f"settings.{NEW}.yaml"
    new_env = PROJECT_ROOT / f".env.{NEW}"

    for path in (src_cfg, donor_env):
        if not path.exists():
            sys.exit(f"Missing {path.name}. Run this on the trading server, not a dev machine.")

    breakeven = round(args.take_profit - ARM_BEFORE, 2)
    src_doc = yaml.safe_load(src_cfg.read_text(encoding="utf-8"))
    m3_stop = float(src_doc.get("stop_loss_usd") or 7.0)
    # The source's real top lot, not a hardcoded 0.12: copying from a leg
    # that already sizes at 0.08 would otherwise re-inflate it to 0.12 and
    # silently hand the new account 50% more risk.
    ladder = src_doc.get("position_sizing") or [{"lots": 0.12}]
    m3_lots = float(ladder[-1]["lots"])
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

    # Siblings are the accounts sharing the NEW account's MT5 login, which
    # is the donor's — not the source's. With --source demo1_m5 --donor
    # demo2_m1 the two are different families entirely, and listing the
    # source's magic would leave demo2's real siblings unaware of the new
    # leg: their guards would see its positions as foreign and close them.
    donor_doc = yaml.safe_load((cfg_dir / f"settings.{DONOR}.yaml").read_text(encoding="utf-8"))
    donor_magic = int(donor_doc["execution"]["magic_number"])
    family = [donor_magic] + [int(m) for m in
                              donor_doc["execution"].get("sibling_magic_numbers", [])]
    magic = args.magic or donor_magic + 4
    src_magic = family[1] if len(family) > 1 else donor_magic

    # Build the config by editing the PARSED document and re-serialising it.
    # The first version did string substitution on the YAML text and appended
    # any key it had not found to the end of the file, which put nested keys
    # at top level and produced a file yaml.safe_load could not read at all.
    # Round-tripping through the parser cannot emit a structurally invalid
    # file, which matters more here than preserving the source's comments --
    # the header below carries the explanations instead.
    doc = build_document(
        yaml.safe_load(src_cfg.read_text(encoding="utf-8")),
        timeframe=TIMEFRAME, stop=args.stop, take_profit=args.take_profit,
        breakeven=breakeven, trail=args.trail, lock_below=args.lock_below,
        magic=magic, siblings=family, scale=scale,
        price_scale=args.stop / m3_stop,
    )

    header = (
        f"# {NEW} — M5 leg. Same rules as {SOURCE}; only the dollar levels differ,\n"
        f"# and they were fitted by scripts/fit_new_timeframe.py over 2026-03-10..09-08,\n"
        f"# not scaled by hand. Replaces {DONOR}, whose M1 signal lost money in every\n"
        f"# walk-forward cut.\n"
        f"#\n"
        f"#   stop {args.stop:.2f}      >= 2x the measured M5 median candle range ({2 * M5_MEDIAN_RANGE:.2f}).\n"
        f"#                    A stop inside one candle's ordinary range is hit by noise.\n"
        f"#   take_profit {args.take_profit:.2f}  near M5's median favourable excursion, measured\n"
        f"#                    independently by scripts/timeframe_expectancy.py.\n"
        f"#   breakeven {breakeven:.2f}    DERIVED as take_profit - arm_before. Never set it\n"
        f"#                    above the arm point: on 2026-09-08 M3 ran with a gap there\n"
        f"#                    and a +$5 winner could still fall to -$7.\n"
        f"#   arm_before {ARM_BEFORE:.2f}   NOT scaled with the candle. It races the broker's\n"
        f"#                    take-profit fill, which depends on dollars per SECOND -- the\n"
        f"#                    same market on every chart. demo1_m1's $0.20 lost that race\n"
        f"#                    7 times in 10.\n"
        f"#   runner OFF       bot/backtest/runner.py does not simulate tp_runner, so a\n"
        f"#                    timeframe that has never traded cannot have one fitted.\n"
        f"#                    Enable it later from real exits (simulate_tp_runner.py).\n"
        f"#   lots x{scale:.3f}     keeps risk/trade at ${lots * args.stop * 100:.0f}, matching {SOURCE}.\n"
        f"#\n"
        f"# Secrets live in .env.{NEW}, not here.\n\n"
    )
    body = header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

    print(f"\n  magic_number  : {magic}   (siblings: {', '.join(str(m) for m in family)})")
    print(f"  lot ladder    : scaled x{scale:.3f}, top {m3_lots} -> {lots}")
    print(f"  price levels  : scaled x{args.stop / m3_stop:.3f} from {SOURCE} "
          f"(breakeven_lock too)")
    print(f"  writes        : {new_cfg.name}, .env.{NEW} (copied from .env.{DONOR}, never read)")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    if new_cfg.exists() and not args.replace:
        sys.exit(f"{new_cfg.name} already exists. Re-run with --replace to overwrite it.")
    new_cfg.write_text(body, encoding="utf-8")
    if not new_env.exists():
        shutil.copyfile(donor_env, new_env)      # bytes only; contents never touched
    # Prove the file we just wrote can actually be loaded, rather than
    # finding out when the bot fails to start.
    yaml.safe_load(new_cfg.read_text(encoding="utf-8"))
    print(f"\n  written and parsed OK: {new_cfg}")
    print(f"  credentials: {new_env}")

    # Every account sharing this login must learn about the new leg, or
    # its duplicate-position guard will treat the new positions as foreign
    # trades and CLOSE them. Updating only the source was enough when
    # source and donor were the same family; with --source demo1_m5
    # --donor demo2_m1 they are not.
    for cfg_path in sorted(cfg_dir.glob("settings.*.yaml")):
        if cfg_path.name.endswith(".example.yaml") or cfg_path == new_cfg:
            continue
        try:
            doc_other = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            other_magic = int(doc_other.get("execution", {}).get("magic_number", 0))
        except (yaml.YAMLError, TypeError, ValueError):
            continue
        if other_magic not in family:
            continue
        sibs = [int(m) for m in doc_other["execution"].get("sibling_magic_numbers", [])]
        if magic in sibs:
            continue
        doc_other["execution"]["sibling_magic_numbers"] = sibs + [magic]
        cfg_path.write_text(yaml.safe_dump(doc_other, sort_keys=False, default_flow_style=False),
                            encoding="utf-8")
        print(f"  updated: {cfg_path.name} sibling_magic_numbers += {magic}")

    if args.retire_m1:
        print(f"\n  retiring {DONOR} (never killed while holding a position):")
        for leg in (f"MT5-Bot-{DONOR}", f"MT5-Bot-Watchdog-{DONOR}"):
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
