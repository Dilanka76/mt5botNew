"""Safely restart one or more account bots so they pick up config changes.

A running bot reads its config once, at startup. Editing
config/settings.<account>.yaml does nothing until the process is
replaced -- see scripts/verify_tp_runner_live.py.

WHY THIS EXISTS RATHER THAN A BARE taskkill: nothing in this project
places a broker-side stop-loss (open_market_order sends only "tp"; every
stop is enforced in software by the polling loop). So killing the bot
while a position is OPEN leaves that position at the broker with a
take-profit and NO STOP AT ALL until the bot comes back. On a $10-stop
M3 trade in a fast market that is a real, avoidable loss.

So this refuses to kill an account that is holding a position, unless
--force is given. Wait for the trade to close, or close it yourself
first.

    python scripts/restart_bot.py --accounts demo1_m1,demo1_m3
    python scripts/restart_bot.py --accounts demo1_m3 --force   # accept the risk

Killing is all this does. Task Scheduler owns relaunching main.py after
its process exits (see scripts/watchdog.py's header), so the bot should
come back on its own; if you drive the bots from the mobile app, turn
the account ON there instead once this reports the process is gone.

ALWAYS follow up with:
    python scripts/verify_tp_runner_live.py
which proves the NEW process actually loaded the new config.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

sys.path.insert(0, ".")

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector
from bot.process_utils import find_account_process

MAIN_SCRIPT_MATCH = "main.py"
WAIT_SECONDS = 15


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    p.add_argument("--force", action="store_true",
                   help="kill even while a position is open (leaves it with NO stop until restart)")
    return p.parse_args()


def open_positions(account: str) -> list:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(symbol=config.symbol) or []
        return [p for p in positions if p.magic == config.execution.magic_number]
    finally:
        connector.disconnect()


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        print("=" * 74)
        print(account)
        print("=" * 74)

        proc = find_account_process(MAIN_SCRIPT_MATCH, account)
        if proc is None:
            print("  No running process found -- nothing to kill. It may already be stopped.")
            print()
            continue
        print(f"  Running: pid={proc['pid']}")

        try:
            positions = open_positions(account)
        except Exception as exc:  # noqa: BLE001 - never let a probe failure kill blindly
            print(f"  Could not check for open positions ({exc}).")
            if not args.force:
                print("  REFUSING to kill without knowing. Re-run with --force to override.")
                print()
                continue
            positions = []

        if positions and not args.force:
            for p in positions:
                print(f"  OPEN POSITION: ticket={p.ticket} {p.volume} lots profit={p.profit:+.2f}")
            print("  REFUSING to kill -- this position has NO broker-side stop and would be")
            print("  left unprotected until the bot restarts. Wait for it to close, then re-run.")
            print()
            continue
        if positions:
            print(f"  {len(positions)} position(s) open -- killing anyway (--force).")
        else:
            print("  Flat, no open position. Safe to restart.")

        result = subprocess.run(["taskkill", "/PID", str(proc["pid"]), "/F", "/T"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  taskkill FAILED: {(result.stderr or result.stdout).strip()}")
            print()
            continue
        print(f"  Killed pid={proc['pid']}. Waiting for it to disappear...")

        for _ in range(WAIT_SECONDS):
            time.sleep(1)
            if find_account_process(MAIN_SCRIPT_MATCH, account) is None:
                print("  Confirmed gone.")
                break
        else:
            print(f"  STILL PRESENT after {WAIT_SECONDS}s -- check manually before restarting.")
        print()

    print("Task Scheduler should relaunch main.py on its own; if you use the mobile app,")
    print("turn the account ON there now. Then confirm the new config really loaded:")
    print("    python scripts/verify_tp_runner_live.py")


if __name__ == "__main__":
    main()
