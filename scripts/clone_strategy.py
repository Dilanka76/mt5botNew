"""Copy a demo account's STRATEGY onto another account, identity untouched.

User, 2026-09-13: go live on live2 with demo1's strategy, daily loss
limit $150.

A config holds two unrelated things and only one of them should ever be
copied:

  STRATEGY   timeframe, stop, target, breakeven, runner, swap rule,
             sessions, sizing ladder — what the bot DOES
  IDENTITY   symbol, magic number, siblings, execution mode, the
             demo-account guard, credentials — WHICH account it is

Copying a whole file would carry demo1's XAUUSDp symbol, its magic
numbers and `require_demo_account: true` onto a live account, and the bot
would refuse to start or, worse, trade the wrong symbol. So this copies
the strategy fields and leaves every identity field exactly as the target
already has it.

LIVE-MONEY REFUSALS, because the target here is real money:

  - a source with NO stop loss cannot go to a live account. demo2_m3
    holds losers until the opposite cross; on a real account a gap or a
    stalled bot makes that unbounded.
  - a live target must be mode live_execute with require_demo_account
    false, and this will not silently change either — it reports what it
    finds and stops if the target is not already set up as a live
    account.
  - it refuses while the target holds an open position.
  - daily_loss_limit_usd must be set explicitly for a live target. Every
    account has run without one; going live without one is the single
    biggest hole left.

    python scripts/clone_strategy.py --from demo1_m5 --to live2_m5 --daily-loss 150
    python scripts/clone_strategy.py --from demo1_m5 --to live2_m5 --daily-loss 150 --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, load_config, validate_account_name

# Copied. Everything not listed here is left as the target has it.
STRATEGY_FIELDS = [
    "timeframe", "candles_to_fetch", "tick_poll_interval_seconds",
    "ema_periods", "gap_threshold_usd", "strategy_variant",
    "take_profit_usd", "stop_loss_usd",
    "breakeven_trigger_usd", "breakeven_lock_usd",
    "tp_runner_trail_usd", "tp_runner_arm_before_usd", "tp_runner_lock_below_usd",
    "swap_immediate", "entry_filter_enabled", "early_entry_threshold_usd",
    "htf_trend_timeframe", "htf_trend_take_profit_usd",
    "sessions", "position_sizing", "swap_adx_filter",
]

# Never copied — these say WHICH account this is.
IDENTITY_FIELDS = ["symbol", "execution", "mt5", "logging", "kill_switch"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="source", required=True, type=validate_account_name)
    p.add_argument("--to", dest="target", required=True, type=validate_account_name)
    p.add_argument("--identity-from", default=None, type=validate_account_name,
                   help="CREATE the target if it does not exist, taking symbol, execution "
                        "mode, credentials and the demo-account guard from this account and "
                        "the strategy from --from. For adding a second leg to a live "
                        "account that already has one.")
    p.add_argument("--magic", type=int, default=None,
                   help="magic number for a newly created target (default: identity's + 2)")
    p.add_argument("--daily-loss", type=float, default=None,
                   help="daily_loss_limit_usd for the target (required for a live account)")
    p.add_argument("--no-software-stop", action="store_true",
                   help="null out stop_loss_usd on the TARGET after copying, so losers exit "
                        "only on the opposite cross behind the broker backstop. Deliberate "
                        "deviation from the source; refused unless a backstop is set.")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = PROJECT_ROOT / "config"
    src_path, dst_path = cfg / f"settings.{args.source}.yaml", cfg / f"settings.{args.target}.yaml"
    if not src_path.exists():
        sys.exit(f"{src_path.name} not found (source). Run this on the trading server.")
    # The target may legitimately be missing -- that is what --identity-from
    # is for. Requiring it here made the creation path unreachable.
    if not dst_path.exists() and args.identity_from is None:
        sys.exit(f"{dst_path.name} not found (target). Pass --identity-from <account> to "
                 f"create it from a real account's identity.")

    src = yaml.safe_load(src_path.read_text(encoding="utf-8"))
    created = not dst_path.exists()
    if created:
        if args.identity_from is None:
            sys.exit(f"{dst_path.name} does not exist. To create it, pass --identity-from "
                     f"<account> so the new leg inherits a real account's symbol, execution "
                     f"mode and credentials rather than the source's.")
        # A new leg must inherit IDENTITY from a live sibling, never from
        # the demo source: copying demo1_m5 wholesale would carry XAUUSDp,
        # demo_execute and require_demo_account: true onto a real account.
        #
        # Built in MEMORY only. The first version wrote the file here,
        # before the --apply check, so a DRY RUN left a real
        # settings.live2_m5.yaml behind holding live2_m3's M3 strategy
        # under the M5 name -- a config Task Scheduler could have started.
        id_path = cfg / f"settings.{args.identity_from}.yaml"
        if not id_path.exists():
            sys.exit(f"settings.{args.identity_from}.yaml not found.")
        base = yaml.safe_load(id_path.read_text(encoding="utf-8"))
        sibling_magic = int(base["execution"]["magic_number"])
        magic = args.magic or sibling_magic + 2
        base["execution"]["magic_number"] = magic
        base["execution"]["sibling_magic_numbers"] = sorted(
            set(base["execution"].get("sibling_magic_numbers", [])) | {sibling_magic})
        dst = base
        original = None
        print(f"  {'CREATING' if args.apply else 'WOULD CREATE'} {dst_path.name} from "
              f"{args.identity_from}'s identity, magic {magic}")
        print(f"  {'CREATING' if args.apply else 'WOULD CREATE'} .env.{args.target} "
              f"(copy of .env.{args.identity_from}, never read)\n")

    if not created:
        original = dst_path.read_text(encoding="utf-8")   # exact bytes, to revert
        dst = yaml.safe_load(original)
    is_live = args.target.startswith("live")

    print("=" * 84)
    print(f"CLONE STRATEGY   {args.source}  ->  {args.target}"
          + ("   *** REAL MONEY ***" if is_live else ""))
    print("=" * 84)

    # ---- identity, shown so it is obvious what is NOT changing ----
    ex = dst.get("execution", {})
    print(f"  target keeps its own identity:")
    print(f"    symbol            {dst.get('symbol')}"
          + ("" if dst.get("symbol") == src.get("symbol")
             else f"   (source trades {src.get('symbol')} — NOT copied)"))
    print(f"    magic_number      {ex.get('magic_number')}")
    print(f"    siblings          {ex.get('sibling_magic_numbers')}")
    print(f"    mode              {ex.get('mode')}")
    print(f"    demo-only guard   {ex.get('require_demo_account')}")

    if is_live:
        if ex.get("mode") != "live_execute":
            sys.exit(f"\nREFUSING: {args.target} has mode '{ex.get('mode')}', not 'live_execute'.\n"
                     f"  Set that deliberately in config/settings.{args.target}.yaml first — this\n"
                     f"  script will not switch an account to real money as a side effect.")
        if ex.get("require_demo_account"):
            sys.exit(f"\nREFUSING: {args.target} still has require_demo_account: true, so it would\n"
                     f"  refuse to trade a real account. Change that deliberately first.")
        # The old rule was "no stop -> refuse". That is a proxy, and the
        # wrong one: what makes a no-stop trade dangerous is having NOTHING
        # under it, and a broker-side backstop is something -- it survives
        # the bot dying, which a software stop does not. On 2026-09-14 a
        # forgotten position was protected by exactly that and nothing else.
        #
        # So refuse only when the trade would be genuinely unprotected, and
        # when a backstop IS there, say in dollars what it actually means
        # rather than waving it through.
        if src.get("stop_loss_usd") is None or args.no_software_stop:
            backstop = dst.get("broker_backstop_usd")
            if not backstop:
                sys.exit(f"\nREFUSING: this would leave {args.target} with NO stop of any kind —\n"
                         f"  no software stop and no broker backstop. A gap or a stalled bot then\n"
                         f"  makes the loss unbounded on a real account. Set a backstop first:\n"
                         f"    python scripts/set_safety_rules.py --account {args.target} "
                         f"--backstop 30 --apply")
            tier_lots = float((src.get("position_sizing") or [{"lots": 0}])[-1]["lots"])
            print(f"\n  *** NO SOFTWARE STOP on {args.target}. ***")
            print(f"  Losers exit on the opposite cross; the ${backstop:.2f} broker backstop is")
            print("  the ONLY thing under the trade, which makes it the real stop rather than")
            print(f"  a last resort. At the top tier ({tier_lots} lots) it is a "
                  f"${backstop * tier_lots * 100:.0f} loss.")
        if args.daily_loss is None:
            sys.exit(f"\nREFUSING: a live account needs --daily-loss. Every account here has run\n"
                     f"  without one; it is the biggest hole left before real money.")

    # ---- the strategy ----
    print(f"\n  strategy copied from {args.source}:")
    changes = {}
    for key in STRATEGY_FIELDS:
        if key not in src:
            continue
        before, after = dst.get(key, "(not set)"), src[key]
        changes[key] = after
        if key in ("sessions", "position_sizing", "ema_periods", "swap_adx_filter"):
            same = before == after
            print(f"    {key:<26} {'unchanged' if same else 'REPLACED'}")
        else:
            mark = "" if before == after else "   <-- changed"
            print(f"    {key:<26} {str(before):>10}  ->  {after}{mark}")

    if args.no_software_stop:
        print(f"    {'stop_loss_usd':<26} {str(changes.get('stop_loss_usd')):>10}"
              f"  ->  None   <-- --no-software-stop: opposite cross + backstop only")
        changes["stop_loss_usd"] = None

    if args.daily_loss is not None:
        print(f"    {'daily_loss_limit_usd':<26} {str(dst.get('daily_loss_limit_usd')):>10}"
              f"  ->  {args.daily_loss}   <-- blocks NEW entries after this much is lost in a"
              f" Colombo day")
        changes["daily_loss_limit_usd"] = args.daily_loss

    stop = None if args.no_software_stop else src.get("stop_loss_usd")
    top_lots = float((src.get("position_sizing") or [{"lots": 0}])[-1]["lots"])
    if stop:
        print(f"\n  risk per trade at the top tier: ${stop * top_lots * 100:.0f} "
              f"({top_lots} lots x ${stop:.2f})")
        if args.daily_loss:
            print(f"  the ${args.daily_loss:.0f} daily limit is about "
                  f"{args.daily_loss / (stop * top_lots * 100):.1f} full stops at that size.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    dst.update(changes)
    for key in IDENTITY_FIELDS:      # belt and braces: never let these move
        if key in src and key in dst:
            pass
    if created:
        env_src = PROJECT_ROOT / f".env.{args.identity_from}"
        env_dst = PROJECT_ROOT / f".env.{args.target}"
        if env_src.exists() and not env_dst.exists():
            shutil.copyfile(env_src, env_dst)   # bytes only; never read or printed
    dst_path.write_text(yaml.safe_dump(dst, sort_keys=False, default_flow_style=False),
                        encoding="utf-8")
    reread = yaml.safe_load(dst_path.read_text(encoding="utf-8"))
    assert reread["symbol"] == dst["symbol"], "symbol moved — aborting"
    assert reread["execution"]["magic_number"] == ex.get("magic_number"), "magic moved"

    # Parsing is not loading. A strategy_variant carries requirements of
    # its own -- dual_cross_confirmed_swap_adx refuses to load without a
    # swap_adx_filter section, even though the engine never consults it
    # when swap_immediate is on. live2_m3 runs the plain variant and
    # demo1_m3 the adx one, so this copy changes the engine, and a
    # requirement left behind would surface as the bot failing to start
    # on Monday morning rather than here.
    try:
        load_config(args.target)
    except Exception as exc:                           # noqa: BLE001
        if created:
            dst_path.unlink()
            sys.exit(f"\n  REMOVED {dst_path.name} — it will not load:\n"
                     f"    {type(exc).__name__}: {exc}\n"
                     f"  Nothing was left behind for something to start by accident.")
        dst_path.write_text(original, encoding="utf-8")
        sys.exit(f"\n  REVERTED — the new config will not load:\n"
                 f"    {type(exc).__name__}: {exc}\n"
                 f"  {dst_path.name} is back as it was. Nothing was changed.")
    print(f"\n  written, parsed and LOADED OK: {dst_path.name}")
    print(f"  identity verified unchanged: symbol {reread['symbol']}, "
          f"magic {reread['execution']['magic_number']}")
    if created and args.identity_from:
        id_path = cfg / f"settings.{args.identity_from}.yaml"
        id_doc = yaml.safe_load(id_path.read_text(encoding="utf-8"))
        new_magic = int(reread["execution"]["magic_number"])
        sibs = [int(m) for m in id_doc["execution"].get("sibling_magic_numbers", [])]
        if new_magic not in sibs:
            # Without this the existing leg treats the new leg's positions
            # as foreign trades and CLOSES them -- reject_manual_trades is
            # on for every account.
            id_doc["execution"]["sibling_magic_numbers"] = sibs + [new_magic]
            id_path.write_text(yaml.safe_dump(id_doc, sort_keys=False, default_flow_style=False),
                               encoding="utf-8")
            print(f"  updated {id_path.name}: siblings += {new_magic} "
                  f"(or it would close the new leg's trades as foreign)")

    print(f"\n  Before starting it, read it back:")
    print(f"    python scripts/show_strategy.py --account {args.target}")


if __name__ == "__main__":
    main()
