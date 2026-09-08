"""Is the TP-runner actually making the wins bigger?

POINT 1 of the four structural fixes (2026-09-08). The strategy's
average loss ($30.46) is bigger than its average win ($25.83), so it
needs 54.1% wins to break even and is running at 52.6%. Raising the
average WIN is one of only four ways out. The TP-runner is that attempt:
on reaching take-profit the trade is not closed, the stop locks at the
target, and it trails $2 behind (see [[project_tp_runner]]).

This measures whether it works, on real trades, since it went live on
demo1 on 2026-09-07.

For every `tp_runner_locked` event it finds the real trade, compares
what that trade ACTUALLY earned against the flat take-profit it would
have taken before, and totals the difference. That total is the whole
answer: positive means the runners are paying, negative means the
give-back on stopped-out runners costs more than the runs earn.

Also reports how many locked trades went on to TRAIL (the ones that
actually ran) versus how many stopped straight back at the lock -- the
simulation predicted roughly 1 in 10 would run, and that ratio is what
the whole idea rests on.

    python scripts/check_tp_runner_results.py
    python scripts/check_tp_runner_results.py --since "2026-09-07 00:00:00"

Events logged before 2026-09-08 carry no ticket (added later), so those
are paired by time window instead: the locked event must fall between a
trade's entry and exit. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
USD_PER_LOT_PER_DOLLAR = 100.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    p.add_argument("--since", default="2026-09-07 00:00:00", help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--offset-hours", type=float, default=None)
    return p.parse_args()


def runner_events(account: str, since: datetime) -> dict[str, list[dict]]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out: dict[str, list[dict]] = {"tp_runner_armed": [], "tp_runner_locked": [], "tp_runner_trailed": []}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            action = e.get("action")
            if action in out and ts >= since:
                out[action].append({**e, "_ts": ts})
    return out


def find_trade(trades: list[dict], event_ts: datetime) -> dict | None:
    """The trade that was OPEN when this event fired."""
    for t in trades:
        entry = t["entry_time"].astimezone(timezone.utc)
        exit_ = t["exit_time"].astimezone(timezone.utc)
        if entry <= event_ts <= exit_ + timedelta(seconds=5):
            return t
    return None


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    grand_diff = 0.0
    grand_locked = 0
    grand_ran = 0

    for account in accounts:
        config = load_config(account)
        events = runner_events(account, since)

        print("=" * 84)
        print(f"{account} ({config.timeframe})   TP ${config.take_profit_usd:.2f}, "
              f"trail ${config.tp_runner_trail_usd if config.tp_runner_trail_usd else 0:.2f}")
        print("=" * 84)

        if not events["tp_runner_locked"]:
            armed = len(events["tp_runner_armed"])
            print(f"  No trade has reached the target and locked yet."
                  f"{f'  ({armed} armed but did not reach TP)' if armed else ''}")
            print("  Nothing to measure. The rule only acts on winning trades, so it needs")
            print("  wins to occur before it can show anything.\n")
            continue

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
        finally:
            connector.disconnect()

        trailed_tickets = {e.get("ticket") for e in events["tp_runner_trailed"] if e.get("ticket")}
        trailed_times = [e["_ts"] for e in events["tp_runner_trailed"]]

        print(f"  {'locked at (Colombo)':<22}{'dir':<6}{'baseline $':>12}{'actual $':>11}{'diff $':>10}  ran?")
        rows = []
        for e in events["tp_runner_locked"]:
            trade = find_trade(trades, e["_ts"])
            if trade is None:
                print(f"  {e['_ts'].astimezone(COLOMBO):%H:%M:%S}            "
                      f"(still open, or no matching closed trade yet)")
                continue
            volume = float(trade["volume"])
            baseline = config.take_profit_usd * volume * USD_PER_LOT_PER_DOLLAR
            actual = float(trade["profit"])
            diff = actual - baseline
            entry_t = trade["entry_time"].astimezone(timezone.utc)
            exit_t = trade["exit_time"].astimezone(timezone.utc)
            ran = (trade.get("ticket") in trailed_tickets
                   or any(entry_t <= t <= exit_t for t in trailed_times))
            rows.append({"diff": diff, "ran": ran})
            print(f"  {e['_ts'].astimezone(COLOMBO):%H:%M:%S}            "
                  f"{str(trade['direction']):<6}{baseline:>12.2f}{actual:>11.2f}{diff:>+10.2f}"
                  f"  {'YES' if ran else 'no'}")

        if not rows:
            print("\n  No locked trade has closed yet.\n")
            continue

        total = sum(r["diff"] for r in rows)
        ran = sum(1 for r in rows if r["ran"])
        grand_diff += total
        grand_locked += len(rows)
        grand_ran += ran
        print(f"\n  {len(rows)} locked and closed, {ran} actually ran further "
              f"({100 * ran / len(rows):.0f}%)")
        print(f"  Net vs the old flat take-profit: ${total:+.2f}")
        print(f"  -> the runner has {'ADDED' if total > 0 else 'COST'} ${abs(total):.2f} so far\n")

    print("=" * 84)
    if grand_locked:
        print(f"ALL ACCOUNTS: {grand_locked} locked trades, {grand_ran} ran further "
              f"({100 * grand_ran / grand_locked:.0f}%), net ${grand_diff:+.2f}")
        print()
        print("The simulation predicted ~1 in 10 would run, and that those few would carry")
        print("the entire gain. Judge the RUN RATE as much as the dollars -- a handful of")
        print("trades either way is noise, and this needs weeks, not days.")
    else:
        print("Nothing has locked yet. Re-run once demo1 has had some winning trades.")


if __name__ == "__main__":
    main()
