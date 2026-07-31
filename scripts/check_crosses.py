"""Verification tool: prints every EMA13/21 cross found in recent XAUUSD
history so it can be checked by eye against the MT5 chart.

Not part of the trading bot itself — run this manually on the EC2 server:
    python scripts/check_crosses.py

For each cross it prints the candle time, direction, close, EMA13, EMA21,
and the calculated gap (see bot.strategy.cross_detector.calculate_gap) so you
can confirm both the cross timing and the gap math against your own chart
before any entry/exit logic gets wired up.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config
from bot.data.market_data import get_ohlc
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import calculate_gap, detect_all_crosses


def main() -> None:
    config = load_config()

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc(connector, config.symbol, config.timeframe, config.candles_to_fetch)
        df = compute_emas(df, config.ema_periods)
        crosses = detect_all_crosses(df)

        print(f"{config.symbol} {config.timeframe} — {len(df)} candles pulled, {len(crosses)} crosses found\n")
        print(f"{'time':<20} {'dir':<5} {'close':>10} {'ema13':>10} {'ema21':>10} {'gap':>8}")
        for event in crosses:
            gap = calculate_gap(event)
            print(
                f"{str(event.candle_time):<20} {event.direction.value:<5} "
                f"{event.close:>10.2f} {event.ema13:>10.2f} {event.ema21:>10.2f} {gap:>8.2f}"
            )
    finally:
        connector.disconnect()


if __name__ == "__main__":
    main()
