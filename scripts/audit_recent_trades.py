"""The last N trades, event by event, checked against the config.

User's question 2026-09-08: *"tell me the demo1 last 5 trades and the two
running trades -- what happened, why the loss, and look whether our
strategy follows exactly correct."*

Two different things, and this does both:

  WHAT HAPPENED -- every decision the engine logged for that trade, in
  order: the entry and its reason, the breakeven arming, each TP-runner
  stage, and the exit. Read top to bottom it is the trade's whole story.

  DID IT FOLLOW THE RULES -- the levels are recomputed from the account's
  CURRENT config and compared with what the engine actually did:
    - the stop placed at entry vs entry -/+ stop_loss_usd
    - the breakeven, if it armed, vs entry -/+ breakeven_lock_usd
    - the runner lock vs take_profit_usd - tp_runner_lock_below_usd
  A mismatch is printed as MISMATCH with both numbers, so a rule that
  quietly is not being applied shows up instead of being assumed.

  CAVEAT: config is read as it stands NOW. A trade taken before a config
  change is judged against the new rule and may show a mismatch that was
  correct at the time. Entry times are shown so that is visible -- today,
  anything before 09:49 UTC predates the current demo1 settings.

Open positions are shown too, with the same level checks.

    python scripts/audit_recent_trades.py --accounts demo1_m1,demo1_m3 --count 5

Read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
TRADE_ACTIONS = {"trade_entered", "trade_exited", "trade_closed_tp", "breakeven_armed",
                 "tp_runner_armed", "tp_runner_locked", "tp_runner_trailed",
                 "swap_blocked_low_adx", "entry_blocked_existing_position",
                 "daily_loss_limit_hit", "entry_filtered"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--offset-hours", type=float, default=None)
    return p.parse_args()


def read_events(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = datetime.fromisoformat(e["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if e.get("action") in TRADE_ACTIONS:
                out.append(e)
    out.sort(key=lambda e: e["_ts"])
    return out


def near(a: float, b: float, tol: float = 0.05) -> bool:
    return abs(a - b) <= tol


def check_levels(c, direction: str, entry: float, events: list[dict]) -> None:
    """Recompute what the config says each level should be, and compare."""
    is_buy = direction == "BUY"
    sign = 1 if is_buy else -1

    entered = next((e for e in events if e.get("action") == "trade_entered"), None)
    if entered is not None and entered.get("stop_loss") is not None:
        want = entry - sign * c.stop_loss_usd
        got = float(entered["stop_loss"])
        ok = near(got, want)
        print(f"      stop at entry     : {got:.2f}   expected {want:.2f} "
              f"(entry {'-' if is_buy else '+'} ${c.stop_loss_usd:.2f})   "
              f"{'OK' if ok else '<-- MISMATCH'}")

    be = next((e for e in events if e.get("action") == "breakeven_armed"), None)
    if be is not None:
        want = entry + sign * (c.breakeven_lock_usd or 0.0)
        # This line printed a bare "OK" -- it computed the expected stop and
        # never compared it to the one the bot actually set. On 2026-09-10 a
        # trade whose stop moved to entry+$0.50 was reported OK against an
        # expected entry+$4.50. The check had never failed because it could
        # not fail, and "all rule checks passed" partly rested on it.
        moved = re.search(r"stop-loss moved to ([\d.]+)", be.get("reason", "") or "")
        got = float(moved.group(1)) if moved else None
        ok = got is not None and near(got, want)
        actual = f"{got:.2f}" if got is not None else "unreadable"
        print(f"      breakeven armed   : {actual}   expected {want:.2f} "
              f"(entry {'+' if is_buy else '-'} ${c.breakeven_lock_usd or 0:.2f}, "
              f"trigger ${c.breakeven_trigger_usd:.2f})   "
              f"{'OK' if ok else '<-- MISMATCH'}")
    elif c.breakeven_trigger_usd is not None:
        print(f"      breakeven         : never armed (never reached "
              f"+${c.breakeven_trigger_usd:.2f})")

    locked = next((e for e in events if e.get("action") == "tp_runner_locked"), None)
    if locked is not None:
        lock_profit = c.take_profit_usd - c.tp_runner_lock_below_usd
        want = entry + sign * lock_profit
        got = float(locked.get("locked_at", want))
        ok = near(got, want)
        trailed = sum(1 for e in events if e.get("action") == "tp_runner_trailed")
        print(f"      TP-runner locked  : {got:.2f}   expected {want:.2f} "
              f"(+${lock_profit:.2f})   {'OK' if ok else '<-- MISMATCH'}"
              f"{f'   then trailed {trailed}x' if trailed else '   never trailed'}")
    elif c.tp_runner_trail_usd is not None:
        print(f"      TP-runner         : never locked (never reached the "
              f"${c.take_profit_usd:.2f} target)")


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        c = load_config(account)
        events = read_events(account)

        connector = MT5Connector(c.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, c.symbol))
            trades = get_closed_trades_range(c.symbol, c.execution.magic_number,
                                             now - timedelta(days=3), now, offset)
            import MetaTrader5 as mt5
            open_now = [p for p in (mt5.positions_get(symbol=c.symbol) or [])
                        if p.magic == c.execution.magic_number]
        finally:
            connector.disconnect()

        print("=" * 84)
        print(f"{account} ({c.timeframe})   TP ${c.take_profit_usd:.2f}  stop ${c.stop_loss_usd:.2f}  "
              f"breakeven {c.breakeven_trigger_usd}  runner trail {c.tp_runner_trail_usd} "
              f"lock_below {c.tp_runner_lock_below_usd}  swap_immediate={c.swap_immediate}")
        print("=" * 84)

        trades.sort(key=lambda t: t["entry_time"])
        for t in trades[-args.count:]:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            span = [e for e in events
                    if entry_utc - timedelta(seconds=90) <= e["_ts"] <= exit_utc + timedelta(seconds=30)]
            move = ((float(t["exit_price"]) - float(t["entry_price"])) if t["direction"] == "BUY"
                    else (float(t["entry_price"]) - float(t["exit_price"])))

            print(f"\n  [{t['entry_time'].astimezone(COLOMBO):%H:%M:%S} Colombo] {t['direction']} "
                  f"{t['volume']} lots   entry {t['entry_price']:.2f} -> exit {t['exit_price']:.2f}   "
                  f"moved ${move:+.2f}   P/L ${t['profit']:+.2f}")
            for e in span:
                reason = (e.get("reason") or "")[:110]
                print(f"      {e['_ts'].astimezone(COLOMBO):%H:%M:%S}  {e['action']:<28} {reason}")
            check_levels(c, t["direction"], float(t["entry_price"]), span)

        if open_now:
            print(f"\n  STILL OPEN ({len(open_now)}):")
            for p in open_now:
                direction = "BUY" if p.type == 0 else "SELL"
                sign = 1 if direction == "BUY" else -1
                print(f"    ticket {p.ticket}  {direction} {p.volume} lots  entry {p.price_open:.2f}  "
                      f"now {p.price_current:.2f}  P/L {p.profit:+.2f}  broker sl={p.sl:.2f} tp={p.tp:.2f}")
                print(f"      software stop should be {p.price_open - sign * c.stop_loss_usd:.2f} "
                      f"(entry {'-' if direction == 'BUY' else '+'} ${c.stop_loss_usd:.2f}); "
                      f"broker sl=0.00 is EXPECTED until the runner locks")
        else:
            print("\n  Nothing open right now.")
        print()

    print("Config is read as it stands NOW, so a trade taken before a config change is")
    print("judged against the NEW rule. Each account's config was last modified at:")
    for account in accounts:
        cfg = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        if cfg.exists():
            when = datetime.fromtimestamp(cfg.stat().st_mtime, tz=timezone.utc)
            print(f"    {account}: {when:%Y-%m-%d %H:%M} UTC "
                  f"({when.astimezone(COLOMBO):%d %b %H:%M} Colombo) — a mismatch on a trade")
            print(f"    {'':<{len(account)}}  before that is expected, not a fault.")


if __name__ == "__main__":
    main()
