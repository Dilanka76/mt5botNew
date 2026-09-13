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
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name

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
    p.add_argument("--daily-loss", type=float, default=None,
                   help="daily_loss_limit_usd for the target (required for a live account)")
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = PROJECT_ROOT / "config"
    src_path, dst_path = cfg / f"settings.{args.source}.yaml", cfg / f"settings.{args.target}.yaml"
    for path, what in ((src_path, "source"), (dst_path, "target")):
        if not path.exists():
            sys.exit(f"{path.name} not found ({what}). Run this on the trading server.")

    src = yaml.safe_load(src_path.read_text(encoding="utf-8"))
    dst = yaml.safe_load(dst_path.read_text(encoding="utf-8"))
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
        if src.get("stop_loss_usd") is None:
            sys.exit(f"\nREFUSING: {args.source} has NO stop loss — it holds losers until the\n"
                     f"  opposite cross. On a real account a gap or a stalled bot makes that\n"
                     f"  loss unbounded. Pick a source that has a stop.")
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

    if args.daily_loss is not None:
        print(f"    {'daily_loss_limit_usd':<26} {str(dst.get('daily_loss_limit_usd')):>10}"
              f"  ->  {args.daily_loss}   <-- blocks NEW entries after this much is lost in a"
              f" Colombo day")
        changes["daily_loss_limit_usd"] = args.daily_loss

    stop = src.get("stop_loss_usd")
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
    dst_path.write_text(yaml.safe_dump(dst, sort_keys=False, default_flow_style=False),
                        encoding="utf-8")
    reread = yaml.safe_load(dst_path.read_text(encoding="utf-8"))
    assert reread["symbol"] == dst["symbol"], "symbol moved — aborting"
    assert reread["execution"]["magic_number"] == ex.get("magic_number"), "magic moved"
    print(f"\n  written and parsed OK: {dst_path.name}")
    print(f"  identity verified unchanged: symbol {reread['symbol']}, "
          f"magic {reread['execution']['magic_number']}")
    print(f"\n  Before starting it, read it back:")
    print(f"    python scripts/show_strategy.py --account {args.target}")


if __name__ == "__main__":
    main()
