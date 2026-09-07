"""Creates live2_m1 / live2_m3 as exact copies of the RUNNING demo2 legs.

User's instruction 2026-09-07: "I need to setup live2 looks like demo2
exactly same."

Copies from the LIVE config files on this machine, deliberately not from
anything in git: the repo's config/settings.demo2_m1.yaml says
breakeven_trigger_usd is null while the running demo2_m1 has $4.50. The
repo copy is stale, and a live account built from it would silently
differ from the strategy it is meant to mirror.

ONLY these fields differ from demo2, and each is a safety requirement:

  magic_number            950001 / 950003 -- must be unique, or this bot
                          would adopt and manage demo2's positions.
  sibling_magic_numbers   points at the other live2 leg, mirroring how
                          demo2_m1/demo2_m3 reference each other (both
                          legs share one MT5 login).
  require_demo_account    false. This guard REFUSES to trade on a
                          non-demo account, so it must be off here -- it
                          is the single most dangerous line in the file
                          and is flipped deliberately, not by accident.
  mode                    live_execute. Real orders, and it also makes
                          api_server.py exclude live2 from the mobile
                          app's stop-all/start-all, exactly as live1 is
                          excluded (see api_server.py's /stop-all).
  order_comment           marks the trades as live2's at the broker.

Everything else -- symbol, timeframe, EMAs, gap threshold, take-profit,
stop-loss, breakeven, sessions, lot tiers, strategy_variant -- is copied
byte-for-byte from the running demo2 leg. No TP-runner: demo2 does not
have it, it has never fired on a real trade, and live2 must mirror what
is proven.

    python scripts/create_live2_config.py            # show what it would write
    python scripts/create_live2_config.py --write

Refuses to overwrite an existing live2 config unless --force.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, discover_configured_accounts

PAIRS = [("demo2_m1", "live2_m1", 950001, 950003),
         ("demo2_m3", "live2_m3", 950003, 950001)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true", help="actually write the files")
    p.add_argument("--force", action="store_true", help="overwrite an existing live2 config")
    return p.parse_args()


def load_raw(account: str) -> dict:
    path = PROJECT_ROOT / f"config/settings.{account}.yaml"
    if not path.is_file():
        raise SystemExit(f"{path} not found -- run this on the server, where demo2's real config lives.")
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def existing_magics() -> dict[int, str]:
    used: dict[int, str] = {}
    for account in discover_configured_accounts():
        try:
            raw = load_raw(account)
        except SystemExit:
            continue
        magic = (raw.get("execution") or {}).get("magic_number")
        if magic is not None:
            used[int(magic)] = account
    return used


def main() -> None:
    args = parse_args()
    used = existing_magics()
    print("magic numbers already in use:")
    for magic, account in sorted(used.items()):
        print(f"  {magic}  {account}")
    print()

    for source, target, magic, sibling in PAIRS:
        raw = copy.deepcopy(load_raw(source))
        out_path = PROJECT_ROOT / f"config/settings.{target}.yaml"

        if magic in used:
            raise SystemExit(
                f"REFUSING: magic {magic} is already used by {used[magic]}. Two accounts sharing a "
                f"magic number would make each bot adopt and manage the other's positions."
            )

        before = {
            "magic_number": (raw.get("execution") or {}).get("magic_number"),
            "require_demo_account": (raw.get("execution") or {}).get("require_demo_account"),
            "mode": (raw.get("execution") or {}).get("mode"),
        }

        raw["execution"]["magic_number"] = magic
        raw["execution"]["sibling_magic_numbers"] = [sibling]
        raw["execution"]["require_demo_account"] = False
        raw["execution"]["mode"] = "live_execute"
        raw["execution"]["order_comment"] = "live2-dual-cross-confirmed-swap"
        # Explicit and off. The rule is built and tested; it stays inert
        # until a real number is chosen (user deferred it 2026-09-07).
        raw.setdefault("daily_loss_limit_usd", None)

        print("=" * 74)
        print(f"{target}   (copied from the RUNNING {source})")
        print("=" * 74)
        print(f"  CHANGED  magic_number         {before['magic_number']} -> {magic}")
        print(f"  CHANGED  sibling_magic_numbers -> [{sibling}]")
        print(f"  CHANGED  require_demo_account  {before['require_demo_account']} -> False   <-- real money")
        print(f"  CHANGED  mode                  {before['mode']} -> live_execute")
        print(f"  CHANGED  order_comment         -> live2-dual-cross-confirmed-swap")
        print(f"  copied   symbol={raw.get('symbol')} timeframe={raw.get('timeframe')} "
              f"variant={raw.get('strategy_variant')}")
        print(f"  copied   TP=${raw.get('take_profit_usd')} SL=${raw.get('stop_loss_usd')} "
              f"gap=${raw.get('gap_threshold_usd')} breakeven={raw.get('breakeven_trigger_usd')}")
        print(f"  copied   sessions={raw.get('sessions')}")
        print(f"  copied   lot tiers={raw.get('position_sizing')}")
        print(f"  off      daily_loss_limit_usd={raw.get('daily_loss_limit_usd')}  "
              f"tp_runner_trail_usd={raw.get('tp_runner_trail_usd')}")

        if not args.write:
            print(f"  (dry run -- nothing written. Re-run with --write.)\n")
            continue
        if out_path.exists() and not args.force:
            print(f"  REFUSING: {out_path} already exists. Use --force to overwrite.\n")
            continue

        header = (
            f"# {target} — REAL MONEY. Generated by scripts/create_live2_config.py\n"
            f"# from the RUNNING {source} config, so it mirrors the proven demo2\n"
            f"# strategy exactly. Only the execution block differs: unique magic\n"
            f"# number, require_demo_account false, mode live_execute.\n"
            f"# Secrets live in .env.{target}, not here.\n"
            f"#\n"
            f"# No TP-runner: demo2 does not have it and it has never fired on a\n"
            f"# real trade. daily_loss_limit_usd is present but null — set it to a\n"
            f"# dollar amount to cap a bad day.\n\n"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
        print(f"  WROTE {out_path}\n")
        used[magic] = target

    if args.write:
        print("Next: confirm .env.live2_m1 and .env.live2_m3 exist and hold the LIVE")
        print("login/server, then run:  python scripts/verify_live2.py")


if __name__ == "__main__":
    main()
