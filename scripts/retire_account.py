"""Retire an account so that NOTHING can start it trading again.

User's standing rule, 2026-09-10: *"we cannot trade the 1m, only 3m and
the 5m working on this"*.

On 2026-09-09 demo1_m1 and demo2_m1 were "retired" by stopping the
process and disabling their Task Scheduler entries. Both were trading
again by 12:12 the next day. The tasks were still disabled -- Windows was
not starting them. The mobile app was, because it runs
`main.py --account <name>` directly and has no idea an account was
retired.

There are three ways a bot starts, and stopping it only shut one:

    Task Scheduler   schtasks /Change /DISABLE       closes one
    the mobile app   runs main.py directly           still open
    a person typing  runs main.py directly           still open

So this closes them at the bot's own level, where the launcher cannot
matter:

  1. KILL_SWITCH_<account>  -- checked on every loop; the bot starts,
     sees it, and refuses to trade. Immediate, and survives any launcher.
  2. settings.<account>.yaml -> .retired  -- load_config cannot find it,
     so `main.py --account <name>` fails outright instead of starting.
  3. .env.<account> -> .retired  -- same, for the credentials.
  4. the Task Scheduler entries, disabled as before.

Belt and braces on purpose: the kill switch takes effect instantly on a
bot that is ALREADY running, while the renames stop a new one starting.
Neither alone covers both.

REFUSES while a position is open -- retiring an account holding a trade
would leave it with no bot to manage its stop.

    python scripts/retire_account.py --accounts demo1_m1,demo2_m1
    python scripts/retire_account.py --accounts demo1_m1,demo2_m1 --apply
    python scripts/retire_account.py --accounts demo1_m1 --undo --apply
"""
from __future__ import annotations

import argparse
import subprocess
import sys

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    p.add_argument("--undo", action="store_true", help="bring a retired account back")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="retire even while a position is open (it would be left unmanaged)")
    return p.parse_args()


def open_positions(account: str) -> tuple[str, int]:
    """("ok", n) | ("already-retired", 0) | ("unreadable", 0).

    The first version called connector.positions_get(), which does not
    exist -- MT5Connector has no such method. The AttributeError was
    swallowed by a bare except and the guard reported "could not read
    positions" and carried on, which is a safety check that cannot fail.
    Exactly the fault found in audit_recent_trades' breakeven check the
    same morning.

    The three outcomes are kept apart because they mean different things:
    a missing config is an account already retired and safe to proceed on,
    while a real failure to read must STOP the retirement -- otherwise a
    broker hiccup silently becomes "no positions open".
    """
    settings = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
    if not settings.exists():
        return ("already-retired", 0)
    try:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            positions = mt5.positions_get(symbol=config.symbol)
            if positions is None:
                # None means the query itself failed; an account with no
                # positions returns an empty tuple.
                return ("unreadable", 0)
            return ("ok", sum(1 for p in positions
                              if p.magic == config.execution.magic_number))
        finally:
            connector.disconnect()
    except Exception as exc:                           # noqa: BLE001
        print(f"  could not query positions: {type(exc).__name__}: {exc}")
        return ("unreadable", 0)


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]
    cfg_dir = PROJECT_ROOT / "config"

    for account in accounts:
        print("=" * 78)
        print(f"{account}  —  {'UN-retire' if args.undo else 'retire'}")
        print("=" * 78)
        settings = cfg_dir / f"settings.{account}.yaml"
        env = PROJECT_ROOT / f".env.{account}"
        kill = PROJECT_ROOT / f"KILL_SWITCH_{account}"
        actions = []

        if args.undo:
            for path in (settings, env):
                retired = path.with_suffix(path.suffix + ".retired")
                if retired.exists():
                    actions.append((f"restore {retired.name} -> {path.name}",
                                    lambda r=retired, p=path: r.rename(p)))
            if kill.exists():
                actions.append((f"remove {kill.name}", kill.unlink))
        else:
            state, held = open_positions(account)
            if state == "already-retired":
                print("  (no config file — this account is already retired)")
            elif state == "unreadable" and not args.force:
                print("  REFUSING: could not read this account's open positions.")
                print("  Retiring blind could strand a live trade with no bot to manage its")
                print("  stop. Fix the connection and retry, or --force if you are certain.")
                continue
            elif held > 0 and not args.force:
                print(f"  REFUSING: {held} position(s) open. Retiring now would leave the")
                print(f"  trade with no bot to manage its stop. Wait for it to close, or:")
                print(f"    python scripts/restart_bot.py --accounts {account} --wait-for-flat 120")
                continue
            if not kill.exists():
                actions.append((f"create {kill.name}  (stops a RUNNING bot immediately)",
                                lambda k=kill: k.touch()))
            for path in (settings, env):
                if path.exists():
                    retired = path.with_suffix(path.suffix + ".retired")
                    actions.append((f"{path.name} -> {retired.name}  (stops a NEW one starting)",
                                    lambda p=path, r=retired: p.rename(r)))
            for task in (f"MT5-Bot-{account}", f"MT5-Bot-Watchdog-{account}"):
                actions.append((f"disable scheduled task {task}",
                                lambda t=task: subprocess.run(
                                    ["schtasks", "/Change", "/TN", t, "/DISABLE"],
                                    capture_output=True, text=True)))

        if not actions:
            print("  Nothing to do — already in that state.")
            continue
        for label, _ in actions:
            print(f"    {label}")
        if not args.apply:
            print("\n  DRY RUN — nothing done.\n")
            continue
        for label, do in actions:
            try:
                do()
                print(f"    done: {label}")
            except Exception as exc:                   # noqa: BLE001
                print(f"    FAILED: {label} — {type(exc).__name__}: {exc}")
        print()

    if args.apply and not args.undo:
        print("Now stop anything still running:")
        print(f"  python scripts/restart_bot.py --accounts {args.accounts} --wait-for-flat 120")
        print("The kill switch already stops it trading; this just tidies the process away.")


if __name__ == "__main__":
    main()
