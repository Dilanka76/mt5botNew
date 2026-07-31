"""Entry point: connects to MT5 and runs the EMA-cross scalping loop.

Every tick_poll_interval_seconds, this:
  1. Fetches recent OHLC + recomputes EMAs.
  2. If a new candle has closed since the last check, feeds it to the state
     machine (this is where crosses are confirmed and closes/entries/pending
     setups happen — see bot/strategy/state_machine.py).
  3. Feeds the latest live tick to the state machine (EMA5-touch detection
     while pending, and TP-fill detection while in a position).

execution.mode defaults to "shadow" in config/settings.yaml — no real
orders are sent until that's deliberately changed, and even then
require_demo_account refuses to trade on anything but a confirmed demo
account.
"""
from __future__ import annotations

import logging
import time

from bot.config import load_config
from bot.data.market_data import get_ohlc
from bot.execution.trade_executor import TradeExecutor
from bot.indicators.ema import compute_emas
from bot.kill_switch import KillSwitch
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.strategy.state_machine import EMAScalpEngine

logger = logging.getLogger("bot.main")


def run() -> None:
    config = load_config()
    setup_logging(config.logging)

    connector = MT5Connector(config.mt5)
    connector.connect()

    if config.execution.require_demo_account and not connector.is_demo_account():
        connector.disconnect()
        raise RuntimeError(
            "require_demo_account is true but the connected MT5 account is not a demo account. Aborting."
        )

    kill_switch = KillSwitch(config.kill_switch)
    executor = TradeExecutor(config.execution, connector, config.symbol)
    engine = EMAScalpEngine(config, connector, executor)
    engine.reconcile_on_startup()

    logger.info(
        "Bot started: symbol=%s timeframe=%s mode=%s state=%s",
        config.symbol, config.timeframe, config.execution.mode, engine.state.value,
    )

    last_closed_candle_time = None

    try:
        while True:
            if kill_switch.is_active():
                logger.critical("Kill switch is active. Halting evaluation loop.")
                break

            try:
                df = get_ohlc(connector, config.symbol, config.timeframe, config.candles_to_fetch)
                df = compute_emas(df, config.ema_periods)

                latest_closed_time = df.iloc[-2].name
                if latest_closed_time != last_closed_candle_time:
                    engine.on_new_candle(df)
                    last_closed_candle_time = latest_closed_time

                tick = connector.get_tick(config.symbol)
                engine.on_tick(tick)

            except Exception:
                logger.exception("Error in main loop iteration")

            time.sleep(config.tick_poll_interval_seconds)

    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C).")

    finally:
        connector.disconnect()


if __name__ == "__main__":
    run()
