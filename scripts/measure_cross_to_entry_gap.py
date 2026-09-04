"""How much of the take-profit move do we give up by waiting for the
candle to close?

The user's question (2026-09-04), from real manual-trading experience
before this bot existed: when trading by hand he entered as the cross
was forming and reached take-profit sooner; sometimes "the cross point
and the candle has more gap, that margin we lose from the TP." That is
a concrete, measurable claim and it has never been measured.

METHOD -- exact, no tick data needed. During a forming candle the
provisional EMAs are:
    ema13' = P*k13 + ema13_prev*(1-k13),  k13 = 2/(13+1)
    ema21' = P*k21 + ema21_prev*(1-k21),  k21 = 2/(21+1)
where P is the candle's price so far and ema*_prev are the PREVIOUS
candle's closed values. Setting ema13' == ema21' and solving for P gives
the exact price at which the cross occurs:

    P_cross = [ema21_prev*(1-k21) - ema13_prev*(1-k13)] / (k13 - k21)

Everything on the right is known at the previous candle's close, so this
is causal -- it is the same algebra the retired tick-based entry used,
just solved directly instead of sampled.

The gap we pay for confirmation is |entry_price - P_cross|, reported in
dollars and as a percentage of that account's take_profit_usd.

VALIDATION built in: P_cross should fall inside the confirming candle's
own high-low range (the cross really did happen during that candle). The
script reports how often it does; a low rate would mean the model is
wrong and the numbers should not be trusted.

    python scripts/measure_cross_to_entry_gap.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

K13 = 2.0 / (13 + 1)
K21 = 2.0 / (21 + 1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def cross_price(ema13_prev: float, ema21_prev: float) -> float:
    """Exact price at which the provisional EMA13 and EMA21 become equal,
    given the previous candle's closed EMA values."""
    return (ema21_prev * (1 - K21) - ema13_prev * (1 - K13)) / (K13 - K21)


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        row = w.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=5), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        gaps, gaps_win, gaps_loss, in_range = [], [], [], 0
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_confirming_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            pos = df.index.get_loc(ct)
            if pos < 1:
                continue
            prev = df.iloc[pos - 1]
            pc = cross_price(float(prev["ema13"]), float(prev["ema21"]))
            candle = df.loc[ct]
            if float(candle["low"]) <= pc <= float(candle["high"]):
                in_range += 1
            gap = abs(float(t["entry_price"]) - pc)
            gaps.append(gap)
            (gaps_win if t["profit"] > 0 else gaps_loss).append(gap)

        if not gaps:
            print(f"{account}: no matched trades.\n")
            continue

        tp = config.take_profit_usd
        med = statistics.median(gaps)
        mean = statistics.mean(gaps)
        print(f"{'=' * 78}\n{account}: {len(gaps)} trades   (take-profit ${tp:.2f})\n{'=' * 78}")
        print(f"  Gap between the cross price and our actual entry:")
        print(f"    median ${med:.2f}  = {100*med/tp:.1f}% of the take-profit target")
        print(f"    mean   ${mean:.2f}  = {100*mean/tp:.1f}% of the take-profit target")
        srt = sorted(gaps)
        print(f"    best  ${srt[0]:.2f}   |  25th pct ${srt[len(srt)//4]:.2f}   "
              f"|  75th pct ${srt[3*len(srt)//4]:.2f}   |  worst ${srt[-1]:.2f}")
        if gaps_win and gaps_loss:
            print(f"    median gap on WINNING trades: ${statistics.median(gaps_win):.2f}  "
                  f"({len(gaps_win)} trades)")
            print(f"    median gap on LOSING trades:  ${statistics.median(gaps_loss):.2f}  "
                  f"({len(gaps_loss)} trades)")
        print(f"  VALIDATION: cross price fell inside the confirming candle's range "
              f"{in_range}/{len(gaps)} times ({100*in_range/len(gaps):.0f}%)")
        print()


if __name__ == "__main__":
    main()
