"""One-off: shows every decision/trade logged around a given app-time
(Sri Lanka local time) today, for one or more accounts -- used to check
"did the bot see/take this entry?" against a specific real-world moment
the user is looking at directly (their broker terminal, chart, etc.).

Reads BOTH decisions.jsonl (every strategy evaluation -- entries, skips,
swap checks) and trade_history.jsonl (only actually-opened/closed trades),
so it shows the full picture: what the bot considered vs. what it acted on.

Usage:
    python scripts/check_trade_at_time.py demo1_m1 demo2_m1 --time 14:41 --window 5
    python scripts/check_trade_at_time.py demo1_m1 --time 14:41 --date 2026-08-31
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from bot.config import load_config

COLOMBO = ZoneInfo("Asia/Colombo")

parser = argparse.ArgumentParser()
parser.add_argument("accounts", nargs="+")
parser.add_argument("--time", required=True, help="HH:MM, Sri Lanka local (app) time")
parser.add_argument("--date", default=None, help="YYYY-MM-DD, Sri Lanka local date (default: today)")
parser.add_argument("--window", type=int, default=5, help="minutes before/after to include")
args = parser.parse_args()

hh, mm = map(int, args.time.split(":"))
if args.date:
    y, m, d = map(int, args.date.split("-"))
else:
    today = datetime.now(COLOMBO).date()
    y, m, d = today.year, today.month, today.day

target = datetime(y, m, d, hh, mm, tzinfo=COLOMBO)
window = timedelta(minutes=args.window)
start, end = target - window, target + window

print(f"Looking for activity between {start.isoformat()} and {end.isoformat()} (Sri Lanka time)\n")

for account in args.accounts:
    config = load_config(account)
    log_dir = Path(config.logging.log_dir) / account
    print(f"{'=' * 20} {account} {'=' * 20}")

    decisions_path = log_dir / "decisions.jsonl"
    found_decisions = []
    if decisions_path.is_file():
        for line in decisions_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["timestamp"]).astimezone(COLOMBO)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if start <= ts <= end:
                found_decisions.append((ts, entry))

    if found_decisions:
        print(f"-- {len(found_decisions)} decision(s) logged --")
        for ts, entry in found_decisions:
            print(f"  [{ts.strftime('%H:%M:%S')}] action={entry.get('action')} reason={entry.get('reason')}")
            extra = {k: v for k, v in entry.items() if k not in ("timestamp", "symbol", "action", "reason")}
            if extra:
                print(f"    {extra}")
    else:
        print("-- No decisions.jsonl entries in this window --")

    ledger_path = log_dir / "trade_history.jsonl"
    found_trades = []
    if ledger_path.is_file():
        for line in ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trade = json.loads(line)
                open_ts = datetime.fromisoformat(trade["open_time"]).astimezone(COLOMBO)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if start <= open_ts <= end:
                found_trades.append((open_ts, trade))

    if found_trades:
        print(f"-- {len(found_trades)} real trade(s) opened in this window --")
        for open_ts, trade in found_trades:
            print(f"  [{open_ts.strftime('%H:%M:%S')}] {trade.get('direction')} entry={trade.get('entry_price')} "
                  f"profit={trade.get('profit')} close_reason={trade.get('close_reason')}")
    else:
        print("-- No trades opened in this window --")
    print()
