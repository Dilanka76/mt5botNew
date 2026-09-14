"""What is the open trade doing right now, and why has it not closed?

User, 2026-09-14: *"can you check the demo1 live running to trade, that
trade take profit not taken"*.

A trade sitting past its take-profit without closing is the TP-runner
working as designed, not a fault -- but the two look identical from an
MT5 terminal, because the thing the runner does FIRST is delete the
broker take-profit. The position then shows TP 0.00 and just keeps going.

This prints, per account, the open position as the BROKER holds it (entry,
current price, stop, take-profit, floating P/L) next to the runner events
this bot logged for that position, so the two can be read together:

    tp_runner_armed    broker take-profit removed, $1 before the target
    tp_runner_locked   stop moved to the lock level, trade kept open
    tp_runner_trailed  stop ratcheted up behind a new best price

If ARMED is present and take-profit reads 0.00, that is the runner. If
take-profit is still set and price is past it, that is a real problem.

    python scripts/show_open_trade.py
    python scripts/show_open_trade.py --accounts demo1_m3,demo1_m5

Read-only: reads positions and log files. Places and modifies nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

RUNNER_ACTIONS = ("tp_runner_armed", "tp_runner_locked", "tp_runner_trailed")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m3,demo1_m5",
                   help="comma-separated; defaults to the two live demo1 legs")
    p.add_argument("--events", type=int, default=12,
                   help="how many recent runner events to show per account")
    return p.parse_args()


def read_events(log_dir: Path, ticket: int | None, limit: int) -> list[dict]:
    """Runner events from decisions.jsonl, newest last. Filtered to one
    ticket when the position is known, so a previous trade's runner events
    cannot be read as this one's."""
    path = log_dir / "decisions.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue          # a half-written line from a crash mid-append
        if entry.get("action") not in RUNNER_ACTIONS:
            continue
        if ticket is not None and entry.get("ticket") != ticket:
            continue
        out.append(entry)
    return out[-limit:]


def main() -> None:
    args = parse_args()
    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        print("=" * 84)
        print(f"{account}   {config.symbol}   magic {config.execution.magic_number}")
        print("=" * 84)

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            positions = [p for p in (mt5.positions_get(symbol=config.symbol) or [])
                         if p.magic == config.execution.magic_number]
            tick = mt5.symbol_info_tick(config.symbol)
        finally:
            connector.disconnect()

        if not positions:
            print("  no open position")
            print()
            continue

        for pos in positions:
            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            # A BUY is valued at the bid (that is where it would close); a
            # SELL at the ask. Using the wrong side here is a whole spread
            # of error, which on a $6 target is most of a percent.
            now = (tick.bid if is_buy else tick.ask) if tick else pos.price_current
            favorable = (now - pos.price_open) if is_buy else (pos.price_open - now)
            opened = datetime.fromtimestamp(pos.time, tz=timezone.utc)
            age = (datetime.now(timezone.utc) - opened).total_seconds() / 60

            print(f"  ticket {pos.ticket}   {'BUY' if is_buy else 'SELL'}   {pos.volume} lots")
            print(f"  opened               {opened:%Y-%m-%d %H:%M:%S} UTC  ({age:.0f} min ago)")
            print(f"  entry                {pos.price_open:.2f}")
            print(f"  now                  {now:.2f}   ({favorable:+.2f} in price, "
                  f"{pos.profit:+.2f} floating)")
            print(f"  broker stop-loss     {pos.sl:.2f}" if pos.sl else
                  "  broker stop-loss     NONE")
            if pos.tp:
                print(f"  broker take-profit   {pos.tp:.2f}")
            else:
                print("  broker take-profit   REMOVED (0.00)")
            print(f"  configured target    ${config.take_profit_usd:.2f}  "
                  f"-> would have closed at "
                  f"{pos.price_open + config.take_profit_usd if is_buy else pos.price_open - config.take_profit_usd:.2f}")

            events = read_events(PROJECT_ROOT / config.logging.log_dir / account,
                                 pos.ticket, args.events)
            print(f"\n  runner events for ticket {pos.ticket}:")
            if not events:
                print("    none logged for this ticket")
            for e in events:
                print(f"    {e['timestamp'][:19]}  {e['action']:<18} {e['reason']}")

            # ---- the verdict --------------------------------------
            armed = any(e["action"] == "tp_runner_armed" for e in events)
            print("\n  VERDICT:")
            if armed and not pos.tp:
                print("    The TP-runner armed and removed the broker take-profit. This is")
                print("    the feature working -- the trade is being held for a bigger win")
                print(f"    behind a stop at {pos.sl:.2f}, not a missed take-profit.")
            elif not armed and pos.tp and favorable >= config.take_profit_usd:
                print("    PROBLEM: price is past the target and the broker take-profit is")
                print("    still set, but nothing armed. The bot may not be running, or its")
                print("    loop is not reaching _manage_tp_runner. Check the log.")
            elif armed and pos.tp:
                print("    ARMED but the broker take-profit is STILL SET -- the removal was")
                print("    rejected. Harmless (the trade closes at the target as it always")
                print("    did) but the runner is not active on this trade.")
            else:
                print(f"    Trade is {favorable:+.2f} from entry against a "
                      f"${config.take_profit_usd:.2f} target -- it has not reached the")
                print("    arm point yet. Nothing to explain.")
            print()


if __name__ == "__main__":
    main()
