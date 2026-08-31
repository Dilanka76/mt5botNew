"""One-off: shows every decision/trade logged around a given MT5 "app
time" (the broker-server time shown directly in the MetaTrader5 terminal/
chart -- see bot/analytics.mt5_utc_offset's docstring) today, for one or
more accounts. Used to check "did the bot see/take this entry?" against a
specific moment the user is looking at directly on their MT5 chart.

IMPORTANT time-convention gotcha this script exists to handle correctly:
- decisions.jsonl timestamps are TRUE UTC (log_decision uses
  datetime.now(timezone.utc)).
- trade_history.jsonl's close_time is RAW MT5 BROKER TIME, mislabeled
  with a UTC tzinfo (bot/mt5_connector.get_recent_closed_trades does
  datetime.fromtimestamp(exit_deal.time, tz=timezone.utc) directly on the
  broker's own epoch, with no offset correction) -- numerically identical
  to "app time" as shown in the MT5 terminal, close_time only (there is
  no separate entry/open-time field in this ledger).
A single --app-time therefore needs converting two different ways for the
two files. This requires a LIVE MT5 connection (to measure today's real
offset, which can shift with the broker's own DST schedule) -- run this
on the server, not the Mac.

Usage:
    python scripts/check_trade_at_time.py demo1_m1 demo2_m1 --app-time 14:41 --window 5
    python scripts/check_trade_at_time.py demo1_m1 --app-time 14:41 --date 2026-08-31
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
from bot.analytics import mt5_utc_offset
from bot.config import load_config
from bot.mt5_connector import MT5Connector

parser = argparse.ArgumentParser()
parser.add_argument("accounts", nargs="+")
parser.add_argument("--app-time", required=True, help="HH:MM, MT5 broker/app time (as shown on the MT5 chart)")
parser.add_argument("--date", default=None, help="YYYY-MM-DD, app-time date (default: today, UTC-based)")
parser.add_argument("--window", type=int, default=5, help="minutes before/after to include")
args = parser.parse_args()

hh, mm = map(int, args.app_time.split(":"))
if args.date:
    y, m, d = map(int, args.date.split("-"))
else:
    today = datetime.now(timezone.utc).date()
    y, m, d = today.year, today.month, today.day

# The app-time target, using the SAME convention trade_history.jsonl's
# close_time uses: the broker-time clock value, labeled with a UTC tzinfo
# (not actually UTC) -- so this compares directly against close_time with
# no conversion needed.
app_target = datetime(y, m, d, hh, mm, tzinfo=timezone.utc)
window = timedelta(minutes=args.window)
app_start, app_end = app_target - window, app_target + window

for account in args.accounts:
    config = load_config(account)
    log_dir = Path(config.logging.log_dir) / account
    print(f"{'=' * 20} {account} {'=' * 20}")

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = mt5_utc_offset(connector, config.symbol)
    finally:
        connector.disconnect()

    # decisions.jsonl is TRUE UTC -- convert the app-time window to true
    # UTC by SUBTRACTING the measured offset (see mt5_utc_offset's
    # docstring: offset = broker-time-as-utc minus true-utc).
    true_utc_start = app_start - offset
    true_utc_end = app_end - offset

    print(f"  measured offset (app time - true UTC): {offset.total_seconds() / 3600:+.2f}h")
    print(f"  app-time window:  {app_start.strftime('%H:%M:%S')} - {app_end.strftime('%H:%M:%S')}")
    print(f"  true-UTC window:  {true_utc_start.strftime('%H:%M:%S')} - {true_utc_end.strftime('%H:%M:%S')} "
          f"(used against decisions.jsonl)")

    decisions_path = log_dir / "decisions.jsonl"
    found_decisions = []
    if decisions_path.is_file():
        for line in decisions_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if true_utc_start <= ts <= true_utc_end:
                found_decisions.append((ts, entry))

    if found_decisions:
        print(f"  -- {len(found_decisions)} decision(s) logged --")
        for ts, entry in found_decisions:
            app_ts = ts + offset
            print(f"    [app {app_ts.strftime('%H:%M:%S')}] action={entry.get('action')} reason={entry.get('reason')}")
            extra = {k: v for k, v in entry.items() if k not in ("timestamp", "symbol", "action", "reason")}
            if extra:
                print(f"      {extra}")
    else:
        print("  -- No decisions.jsonl entries in this window --")

    # trade_history.jsonl's close_time uses the SAME convention as
    # app_target above -- direct comparison, no offset conversion.
    ledger_path = log_dir / "trade_history.jsonl"
    found_trades = []
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
                close_ts = datetime.fromisoformat(trade["close_time"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if app_start <= close_ts <= app_end:
                found_trades.append((close_ts, trade))

    if found_trades:
        print(f"  -- {len(found_trades)} real trade(s) CLOSED in this window (no open-time field exists in this ledger) --")
        for close_ts, trade in found_trades:
            print(f"    [app {close_ts.strftime('%H:%M:%S')}] {trade.get('direction')} price={trade.get('price')} "
                  f"profit={trade.get('profit')} ticket={trade.get('ticket')}")
    else:
        print("  -- No trades closed in this window --")
    print()
