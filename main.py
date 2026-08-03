"""Entry point: connects to MT5 and runs the EMA-cross scalping loop.

Every tick_poll_interval_seconds, this:
  1. Fetches recent OHLC + recomputes EMAs.
  2. If a new candle has closed since the last check, feeds it to the state
     machine (this is where crosses are confirmed and closes/entries/pending
     setups happen — see bot/strategy/state_machine.py).
  3. Feeds the latest live tick to the state machine (EMA5-touch detection
     while pending, and TP-fill detection while in a position).

Every HEARTBEAT_INTERVAL_SECONDS, a "[HEARTBEAT]" line is logged regardless
of whether anything else happened, so logs/app.log always gets written to
on a predictable schedule — this is what scripts/watchdog.py relies on to
tell a genuinely frozen process apart from a healthy one that's just quiet
because no EMA cross has fired recently.

On startup, refuses to run if another main.py is already active (checked
via bot.process_utils) — the definitive guard against duplicate/conflicting
instances, regardless of which supervisor (Task Scheduler, watchdog.py, the
API's /start) tried to launch a second one.

execution.mode defaults to "shadow" in config/settings.yaml — no real
orders are sent until that's deliberately changed, and even then
require_demo_account refuses to trade on anything but a confirmed demo
account.
"""
from __future__ import annotations

import logging
import sys
import time

from bot.config import load_config
from bot.data.market_data import get_ohlc
from bot.execution.trade_executor import TradeExecutor
from bot.indicators.ema import compute_emas
from bot.kill_switch import KillSwitch
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.process_utils import find_script_process
from bot.sessions import is_within_session
from bot.strategy.state_machine import EMAScalpEngine
from bot.strategy.state_machine_ema5_only import EMA5OnlyEngine

logger = logging.getLogger("bot.main")

HEARTBEAT_INTERVAL_SECONDS = 60
THIS_SCRIPT_MATCH = "main.py"

STRATEGY_ENGINES = {
    "gap_threshold": EMAScalpEngine,
    "ema5_only": EMA5OnlyEngine,
}


def run() -> None:
    config = load_config()
    setup_logging(config.logging)

    existing = find_script_process(THIS_SCRIPT_MATCH)
    if existing is not None:
        logger.error(
            "Another main.py is already running (pid=%s). Refusing to start a duplicate instance. Exiting.",
            existing["pid"],
        )
        sys.exit(1)

    connector = MT5Connector(config.mt5)
    connector.connect()

    if config.execution.require_demo_account and not connector.is_demo_account():
        connector.disconnect()
        raise RuntimeError(
            "require_demo_account is true but the connected MT5 account is not a demo account. Aborting."
        )

    engine_cls = STRATEGY_ENGINES.get(config.strategy_variant)
    if engine_cls is None:
        connector.disconnect()
        raise ValueError(
            f"Unknown strategy_variant '{config.strategy_variant}' in config/settings.yaml. "
            f"Valid options: {list(STRATEGY_ENGINES)}"
        )

    kill_switch = KillSwitch(config.kill_switch)
    executor = TradeExecutor(config.execution, connector, config.symbol)
    engine = engine_cls(config, connector, executor)
    engine.reconcile_on_startup()

    logger.info(
        "Bot started: symbol=%s timeframe=%s mode=%s strategy_variant=%s state=%s",
        config.symbol, config.timeframe, config.execution.mode, config.strategy_variant, engine.state.value,
    )

    last_closed_candle_time = None
    last_heartbeat_at = 0.0

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

                now = time.time()
                if now - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS:
                    session_status = "ACTIVE" if is_within_session(config.sessions) else "WAITING"
                    balance = connector.account_info().balance
                    logger.info(
                        "[HEARTBEAT] state=%s session=%s last_price=%.2f balance=%.2f",
                        engine.state.value, session_status, tick.bid, balance,
                    )
                    last_heartbeat_at = now

            except Exception:
                logger.exception("Error in main loop iteration")

            time.sleep(config.tick_poll_interval_seconds)

    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C).")

    finally:
        connector.disconnect()


if __name__ == "__main__":
    run()
