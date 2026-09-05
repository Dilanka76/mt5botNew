"""Shows every REAL losing trade today (Colombo calendar day, same
day-boundary convention as bot.trade_stats/the dashboard's "today"
stats) across all four demo accounts, with the actual reason behind
each loss pulled from decisions.jsonl (stop-loss hit, tightened stop,
swap reversal, etc.) plus a FULL breakdown against all 7 points from
2026-09-01's entry-quality research (candle color, tick volume, EMA13/21
separation, ATR volatility, broker/app-time hour, candle decisiveness,
gap size) -- so each loss can be understood against everything we've
learned, not just counted.

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

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
APP_TZ = timezone(timedelta(hours=3))
EXCLUDED_WINDOW = (8, 12)  # broker/app-time hours, worst window per 2026-09-01 research


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--date", default=None,
                        help="YYYY-MM-DD Colombo calendar day to report (default: today)")
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
    for e in decisions:
        if e.get("action") in ("trade_exited", "trade_closed_tp") and str(e.get("ticket")) == str(ticket):
            return e
    return None


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str):
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period).mean().shift(1)


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    if args.date:
        today_colombo = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        today_colombo = datetime.now(COLOMBO).date()
    day_start = datetime.combine(today_colombo, datetime.min.time(), tzinfo=COLOMBO).astimezone(timezone.utc)
    # For a past day, stop at that day's own end rather than "now".
    day_end = min(
        datetime.combine(today_colombo, datetime.max.time(), tzinfo=COLOMBO).astimezone(timezone.utc),
        datetime.now(timezone.utc),
    )

    print(f"Colombo calendar day: {today_colombo.isoformat()}\n")

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
            # extra warmup so the rolling volume threshold / ATR have real history
            df = get_ohlc_range(connector, config.symbol, config.timeframe, day_start - timedelta(days=3), day_end)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        atr_series = compute_atr(df)

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
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            entry_local = t["entry_time"].astimezone(COLOMBO).strftime("%H:%M:%S")

            print(f"  [{entry_local} Colombo] {t['direction']} entry={t['entry_price']:.2f} exit={t['exit_price']:.2f} "
                  f"P/L=${t['profit']:+.2f}")
            print(f"    Reason: {exit_decision.get('reason') if exit_decision else '(no matching decisions.jsonl entry)'}")

            if candle_time is None:
                print("    (could not locate the confirming candle for the 7-point breakdown)\n")
                continue

            row = df.loc[candle_time]
            body = abs(row["close"] - row["open"])
            candle_range = row["high"] - row["low"]
            body_ratio = body / candle_range if candle_range > 0 else 0.0
            favor = (row["close"] > row["open"]) if t["direction"] == "BUY" else (row["close"] < row["open"])
            ema_gap = abs(row["ema13"] - row["ema21"])
            volume = float(row.get("tick_volume", 0) or 0)
            pos = df.index.get_loc(candle_time)
            vol_window = df["tick_volume"].iloc[max(0, pos - 499):pos + 1]
            vol_threshold = float(vol_window.quantile(1 / 3))
            app_hour = candle_time.hour  # candle index is already raw broker/app time, see market_data.get_ohlc
            in_excluded_window = EXCLUDED_WINDOW[0] <= app_hour < EXCLUDED_WINDOW[1]
            atr = atr_series.loc[candle_time]
            gap = abs(row["close"] - row["ema13"])

            print(f"    7-point breakdown:")
            print(f"      1. Candle color:     {'AGREED' if favor else 'DISAGREED'} with the trade")
            print(f"      2. Tick volume:      {volume:.0f} (rolling 1/3 threshold: {vol_threshold:.0f}) "
                  f"-> {'LOW (bottom third)' if volume < vol_threshold else 'not low'}")
            print(f"      3. EMA13/21 gap:     {ema_gap:.2f}")
            print(f"      4. ATR-14:           {atr:.2f}" if pd.notna(atr) else "      4. ATR-14:           n/a")
            print(f"      5. Broker/app hour:  {app_hour:02d}:00 -> {'IN excluded 08:00-12:00 window' if in_excluded_window else 'outside excluded window'}")
            print(f"      6. Candle decisiveness (body/range): {body_ratio:.2f}")
            print(f"      7. Gap from EMA13:   {gap:.2f} (rule disabled everywhere -- informational only)")
            print()

    print(f"{'=' * 70}\nGRAND TOTAL: {grand_trades} trades today, {grand_losses} losses, "
          f"combined P/L ${grand_total:+.2f} (all losses combined: ${grand_loss_total:+.2f})\n{'=' * 70}")


if __name__ == "__main__":
    main()
