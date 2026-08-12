"""Verifies MT5's candle timestamps are true UTC by comparing a real,
already-logged live trade's entry time (bot.logging_setup.logger's
datetime.now(timezone.utc) — unimpeachable, the system clock, not MT5)
against the MT5 candles bracketing that same moment.

If a candle's UTC-interpreted timestamp lands within about a minute of
the real trade time, MT5's candle epoch is true UTC — this is the
assumption the backtest engine, session-window gating, and every
Colombo-hour breakdown in trade_stats.py all rely on. If the closest
matching candle is instead offset by a round number of hours, that's
your broker's server-time offset, not UTC, and every hour bucket in a
backtest report would need shifting by that same amount to read as true
Sri Lanka time.

    python scripts/verify_candle_utc.py --account demo1
    python scripts/verify_candle_utc.py --account demo1 --timestamp 2026-08-12T14:03:07

Read-only: connects to MT5 only to fetch historical candles, same as
scripts/backtest.py — never touches the live/demo trading connection or
places any order.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

import MetaTrader5 as mt5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument(
        "--timestamp", default=None,
        help="ISO UTC timestamp of a specific trade_entered/trade_exited event to check "
             "(e.g. 2026-08-12T14:03:07). Defaults to the most recent trade_entered in decisions.jsonl.",
    )
    return parser.parse_args()


def find_trade_time(account: str, override: str | None) -> tuple[datetime, dict]:
    if override:
        ts = datetime.fromisoformat(override)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts, {}

    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    if not path.exists():
        raise SystemExit(f"No decisions.jsonl found at {path}")

    last_entered = None
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("action") == "trade_entered":
            last_entered = entry
    if last_entered is None:
        raise SystemExit(
            f"No trade_entered events found in {path} — place a real trade first, or pass --timestamp."
        )

    return datetime.fromisoformat(last_entered["timestamp"]), last_entered


def main() -> None:
    args = parse_args()
    trade_time, entry = find_trade_time(args.account, args.timestamp)

    print(f"Real trade time (true system UTC, from decisions.jsonl): {trade_time.isoformat()}")
    if entry:
        print(f"  direction={entry.get('direction')} entry_price={entry.get('entry')} reason={entry.get('reason')!r}")
    print()

    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        # Printed explicitly (not just left to MT5Connector's own internal
        # logger.info(), which needs setup_logging() to have a handler
        # attached — this script never calls it) so a wrong-terminal
        # connection is impossible to miss: mt5.initialize() attaches to
        # whatever MT5 terminal it finds when this account's config doesn't
        # pin an explicit terminal_path, which on a machine running 5
        # concurrent terminals (one per account) is not guaranteed to be
        # this account's own.
        info = connector.account_info()
        print(f"Connected to MT5 as: login={info.login} server={info.server!r} balance={info.balance:.2f}")
        print(f"  (expected: this should be {args.account}'s own account/server — cross-check against config/settings.{args.account}.yaml / .env.{args.account})")
        print()

        connector.ensure_symbol(config.symbol)
        timeframe = connector.resolve_timeframe(config.timeframe)
        window_start = trade_time - timedelta(minutes=15)
        window_end = trade_time + timedelta(minutes=2)
        rates = mt5.copy_rates_range(config.symbol, timeframe, window_start, window_end)
    finally:
        connector.disconnect()  # read-only fetch — never touches the live trading connection

    if rates is None or len(rates) == 0:
        raise SystemExit("No candles returned for that window — widen it or double-check the symbol/timeframe.")

    print(f"MT5 {config.symbol} {config.timeframe} candles in that window, timestamps read as UTC:")
    print(f"{'candle time (UTC)':<26}{'open':>10}{'high':>10}{'low':>10}{'close':>10}   delta to trade")
    for r in rates:
        candle_time = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
        delta = trade_time - candle_time
        marker = "  <-- closest" if abs(delta.total_seconds()) < 90 else ""
        print(f"{candle_time.isoformat():<26}{r['open']:>10.2f}{r['high']:>10.2f}{r['low']:>10.2f}{r['close']:>10.2f}   {delta}{marker}")

    print()
    print("If a candle's UTC timestamp lands within about a minute of the real trade time above,")
    print("MT5's candle epoch is true UTC — the backtest's Colombo-hour bucketing is correctly anchored.")
    print("If the closest-matching candle is instead offset by a round number of hours (e.g. 2 or 3),")
    print("that's your broker's server-time offset — every hour bucket in the backtest report would")
    print("need to be shifted by that same amount to read as true Colombo time.")


if __name__ == "__main__":
    main()
