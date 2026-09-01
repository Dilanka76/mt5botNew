"""Shows every REAL losing trade today (Colombo calendar day, same
day-boundary convention as bot.trade_stats/the dashboard's "today"
stats) across all four demo accounts, with the actual reason behind
each loss pulled from decisions.jsonl (stop-loss hit, tightened stop,
swap reversal, etc.) plus the same entry-quality context used in
tonight's research (candle color, tick volume) -- so a loss can be
understood, not just counted.

    python scripts/show_losses_today.py
    python scripts/show_losses_today.py --accounts demo1_m1,demo1_m3

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    return parser.parse_args()


def read_decisions(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    entries = []
    if not path.exists():
        return entries
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = datetime.fromisoformat(e["timestamp"])
                entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda e: e["_ts"])
    return entries


def find_exit_decision(decisions: list[dict], ticket: int) -> dict | None:
    """trade_exited (software stop) or trade_closed_tp (broker TP/close)
    -- either can carry the real ticket, matched exactly."""
    for e in decisions:
        if e.get("action") in ("trade_exited", "trade_closed_tp") and str(e.get("ticket")) == str(ticket):
            return e
    return None


def find_confirming_candle(df, near: datetime, direction: str):
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    today_colombo = datetime.now(COLOMBO).date()
    day_start = datetime.combine(today_colombo, datetime.min.time(), tzinfo=COLOMBO).astimezone(timezone.utc)
    day_end = datetime.now(timezone.utc)

    print(f"Today (Colombo calendar day): {today_colombo.isoformat()}\n")

    grand_total = 0.0
    grand_loss_total = 0.0
    grand_trades = 0
    grand_losses = 0

    for account in accounts:
        config = load_config(account)
        decisions = read_decisions(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, day_start, day_end, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, day_start - timedelta(hours=2), day_end)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        todays = [t for t in trades if t["exit_time"].astimezone(COLOMBO).date() == today_colombo]
        losses = [t for t in todays if t["profit"] < 0]
        total_pl = sum(t["profit"] for t in todays)
        loss_total = sum(t["profit"] for t in losses)

        grand_total += total_pl
        grand_loss_total += loss_total
        grand_trades += len(todays)
        grand_losses += len(losses)

        print(f"{'=' * 70}\n{account}: {len(todays)} trades today, {len(losses)} losses, "
              f"total P/L ${total_pl:+.2f} (losses alone: ${loss_total:+.2f})\n{'=' * 70}")

        if not losses:
            print("  No losses today.\n")
            continue

        for t in sorted(losses, key=lambda x: x["entry_time"]):
            exit_decision = find_exit_decision(decisions, t.get("position_id") or "")
            candle_time = find_confirming_candle(df, t["entry_time"].astimezone(timezone.utc), t["direction"])
            quality = ""
            if candle_time is not None:
                row = df.loc[candle_time]
                if t["direction"] == "BUY":
                    favor = row["close"] > row["open"]
                else:
                    favor = row["close"] < row["open"]
                quality = f", confirming candle {'AGREED' if favor else 'DISAGREED'} with the trade, tick_volume={row.get('tick_volume', '?')}"

            entry_local = t["entry_time"].astimezone(COLOMBO).strftime("%H:%M:%S")
            print(f"  [{entry_local} Colombo] {t['direction']} entry={t['entry_price']:.2f} exit={t['exit_price']:.2f} "
                  f"P/L=${t['profit']:+.2f}{quality}")
            if exit_decision:
                print(f"    Reason: {exit_decision.get('reason', '(no reason logged)')}")
            else:
                print(f"    Reason: (no matching decisions.jsonl entry found for this ticket)")
        print()

    print(f"{'=' * 70}\nGRAND TOTAL: {grand_trades} trades today, {grand_losses} losses, "
          f"combined P/L ${grand_total:+.2f} (all losses combined: ${grand_loss_total:+.2f})\n{'=' * 70}")


if __name__ == "__main__":
    main()
