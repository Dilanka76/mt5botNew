"""Pulls real tick-by-tick data for a specific window and replays
dual_cross's exact tick-based entry check (bot/strategy/state_machine_dual_cross.py's
on_tick, Β§3) against it — for manually verifying whether a genuine
close-based EMA13/21 cross ever produced a qualifying tick, at a
resolution the backtest (1-minute OHLC only) can't show.

    python scripts/inspect_ticks.py --account demo1_m1 --from "2026-08-10 08:33:00" --to "2026-08-10 08:34:00"

Uses the account's own dual_cross.cross_tolerance_usd and ema_periods.
The "previous closed candle" EMA13/21 anchor is taken from the candle
immediately before --from (same candle-timestamp convention as the rest
of this codebase: a candle's index label is its OPEN time). Connects to
MT5 only to read tick/candle history, then disconnects — never touches
live/demo trading, same as scripts/backtest.py.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5
import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import CrossState

TIMEFRAME_MINUTES = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM:SS", UTC')
    parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM:SS", UTC')
    return parser.parse_args()


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    if config.strategy_variant != "dual_cross":
        raise ValueError(f"{args.account} is strategy_variant={config.strategy_variant!r}, not dual_cross — this script only replays dual_cross's entry check.")

    dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = dt_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, dt_from)
        df = compute_emas(df, config.ema_periods)
        prev_candle = df.loc[df.index < dt_from].iloc[-1]
        prev_ema13 = float(prev_candle["ema13"])
        prev_ema21 = float(prev_candle["ema21"])
        prev_state = _classify(prev_ema13, prev_ema21)
        print(f"Anchor candle: {prev_candle.name} close={prev_candle['close']:.2f} ema13={prev_ema13:.4f} ema21={prev_ema21:.4f} state={prev_state}")

        ticks = mt5.copy_ticks_range(config.symbol, dt_from, dt_to, mt5.COPY_TICKS_ALL)
    finally:
        connector.disconnect()

    if ticks is None or len(ticks) == 0:
        print("No ticks returned for this window (broker may not retain tick history this far back, or the window is outside trading hours).")
        return

    k_mid = 2 / (config.ema_periods.mid + 1)
    k_slow = 2 / (config.ema_periods.slow + 1)
    tolerance = config.dual_cross.cross_tolerance_usd

    print(f"symbol={config.symbol} tolerance=${tolerance:.2f} k_mid={k_mid:.6f} k_slow={k_slow:.6f}")
    print(f"{'time':<26} {'bid':>10} {'ask':>10} {'prov13':>12} {'prov21':>12} {'gap':>8} {'flip':>6} {'in_tol':>7} {'WOULD_ENTER':>12}")

    would_enter_count = 0
    for t in ticks:
        tick_time = datetime.fromtimestamp(t["time_msc"] / 1000, tz=timezone.utc)
        bid = float(t["bid"])
        ask = float(t["ask"])
        if bid == 0.0:
            continue
        prov13 = bid * k_mid + prev_ema13 * (1 - k_mid)
        prov21 = bid * k_slow + prev_ema21 * (1 - k_slow)
        prov_state = _classify(prov13, prov21)
        gap = prov13 - prov21
        is_flip = prev_state is not None and prov_state is not None and prev_state != prov_state
        within_tolerance = abs(gap) <= tolerance
        would_enter = is_flip and within_tolerance
        if would_enter:
            would_enter_count += 1
        print(f"{tick_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]:<26} {bid:>10.2f} {ask:>10.2f} {prov13:>12.4f} {prov21:>12.4f} {gap:>8.4f} {str(is_flip):>6} {str(within_tolerance):>7} {str(would_enter):>12}")

    print(f"\n{len(ticks)} ticks in window, {would_enter_count} would have qualified for entry (flip AND within ${tolerance:.2f} tolerance).")


if __name__ == "__main__":
    main()
