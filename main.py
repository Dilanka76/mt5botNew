"""Entry point: connects to MT5 and runs the EMA-cross scalping loop.

Multi-account: this process always runs for exactly one account, given via
the required --account flag (e.g. `python main.py --account demo1`). That
name selects .env.<account>, config/settings.<account>.yaml, logs/<account>/,
and KILL_SWITCH_<account> — see SETUP.md. Run one main.py process per
account (up to 5, per the current setup), each against its own MT5
terminal instance.

Every tick_poll_interval_seconds, this:
  1. Fetches recent OHLC + recomputes EMAs.
  2. If a new candle has closed since the last check, feeds it to the state
     machine (this is where crosses are confirmed and closes/entries/pending
     setups happen — see bot/strategy/state_machine.py).
  3. Feeds the latest live tick to the state machine (EMA5-touch detection
     while pending, and TP-fill detection while in a position).

Every HEARTBEAT_INTERVAL_SECONDS, a "[HEARTBEAT]" line is logged regardless
of whether anything else happened, so logs/<account>/app.log always gets
written to on a predictable schedule — this is what scripts/watchdog.py
relies on to tell a genuinely frozen process apart from a healthy one
that's just quiet because no EMA cross has fired recently.

On startup, refuses to run if another main.py for the SAME account is
already active (checked via bot.process_utils.find_account_process) — the
definitive guard against duplicate/conflicting instances of one account,
regardless of which supervisor (Task Scheduler, watchdog.py, the API's
/start) tried to launch a second one. Different accounts' main.py
processes are expected to run concurrently and never conflict with
each other.

execution.mode defaults to "shadow" in config/settings.<account>.yaml — no
real orders are sent until that's deliberately changed, and even then
require_demo_account refuses to trade on anything but a confirmed demo
account.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc
from bot.execution.trade_executor import TradeExecutor
from bot.indicators.adx import compute_adx
from bot.indicators.ema import compute_emas
from bot.kill_switch import KillSwitch
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.process_utils import find_account_process
from bot.sessions import is_within_session
from bot.status_writer import build_status_payload, status_file_path, write_status_atomic
from bot.strategy.state_machine import EMAScalpEngine
from bot.strategy.state_machine_cross_confirmed import CrossConfirmedEngine
from bot.strategy.state_machine_cross_confirmed_adaptive_tp import CrossConfirmedAdaptiveTPEngine
from bot.strategy.state_machine_dual_cross import DualCrossEngine
from bot.strategy.state_machine_dual_cross_confirmed_entry import DualCrossConfirmedEntryEngine
from bot.strategy.state_machine_dual_cross_tight_exit import DualCrossTightExitEngine
from bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm import DualCrossTightExitSwapConfirmEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx_entryfilter import DualCrossConfirmedSwapAdxEntryFilterEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap import DualCrossConfirmedSwapEngine
from bot.strategy.state_machine_dual_cross_confirmed_adx_m15 import DualCrossConfirmedAdxM15Engine
from bot.trade_ledger import append_new_trades, trade_ledger_path

logger = logging.getLogger("bot.main")

HEARTBEAT_INTERVAL_SECONDS = 60
THIS_SCRIPT_MATCH = "main.py"

STRATEGY_ENGINES = {
    "gap_threshold": EMAScalpEngine,
    "dual_cross": DualCrossEngine,
    "dual_cross_confirmed_entry": DualCrossConfirmedEntryEngine,
    "dual_cross_tight_exit": DualCrossTightExitEngine,
    "dual_cross_tight_exit_swap_confirm": DualCrossTightExitSwapConfirmEngine,
    "dual_cross_confirmed_swap_adx": DualCrossConfirmedSwapAdxEngine,
    "dual_cross_confirmed_swap_adx_entryfilter": DualCrossConfirmedSwapAdxEntryFilterEngine,
    "dual_cross_confirmed_swap": DualCrossConfirmedSwapEngine,
    "dual_cross_confirmed_adx_m15": DualCrossConfirmedAdxM15Engine,
    "cross_confirmed": CrossConfirmedEngine,
    "cross_confirmed_adaptive_tp": CrossConfirmedAdaptiveTPEngine,
}

# Variants that can hold 2 simultaneous opposite-direction positions — real
# (non-shadow) order placement for the second one requires the account to
# actually be in hedging margin mode, or it would just net against the
# first at the broker instead of opening a genuinely separate ticket.
# dual_cross_confirmed_entry, dual_cross_tight_exit,
# dual_cross_tight_exit_swap_confirm, dual_cross_confirmed_swap_adx, and
# dual_cross_confirmed_swap deliberately do NOT belong here — all always close the opposite
# position before/as the new one opens (see each engine's module
# docstring), so none of them ever actually holds two at once and none has
# a hedging-account requirement of its own.
CONCURRENT_POSITION_VARIANTS = {"dual_cross"}


def _format_open_position(position) -> dict | None:
    """Converts a raw MT5 position object (or None) into a plain dict —
    keeps bot/status_writer.py's build_status_payload() free of any MT5
    object dependency. Shape matches what api_server.py's gateway exposes."""
    if position is None:
        return None
    return {
        "ticket": position.ticket,
        "direction": "BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
        "volume": position.volume,
        "price_open": position.price_open,
        "price_current": position.price_current,
        "take_profit": position.tp,
        "profit": position.profit,
        "open_time": datetime.fromtimestamp(position.time, tz=timezone.utc).isoformat(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Runs the EMA-cross scalping bot for one MT5 account.")
    parser.add_argument(
        "--account", required=True, type=validate_account_name,
        help="Account name, e.g. demo1, live1. Selects .env.<account> and config/settings.<account>.yaml.",
    )
    return parser.parse_args()


def run() -> None:
    args = parse_args()

    config = load_config(args.account)
    setup_logging(config.logging, args.account)

    existing = find_account_process(THIS_SCRIPT_MATCH, args.account)
    if existing is not None:
        logger.error(
            "Another main.py for account '%s' is already running (pid=%s). "
            "Refusing to start a duplicate instance. Exiting.",
            args.account, existing["pid"],
        )
        sys.exit(1)

    connector = MT5Connector(config.mt5)
    connector.connect()

    if config.execution.require_demo_account and not connector.is_demo_account():
        connector.disconnect()
        raise RuntimeError(
            "require_demo_account is true but the connected MT5 account is not a demo account. Aborting."
        )

    # dual_cross can hold 2 simultaneous opposite positions — real
    # (non-shadow) order placement requires the account to actually be in
    # hedging margin mode, or the second order would just net against the
    # first at the broker instead of opening a genuinely separate ticket.
    # Hard-abort here rather than discover this via a silently-wrong order;
    # DualCrossEngine._enter() also has a redundant defense-in-depth check
    # right before that second order.
    if (
        config.strategy_variant in CONCURRENT_POSITION_VARIANTS
        and config.execution.mode != "shadow"
        and config.dual_cross.require_hedging_account
        and not connector.is_hedging_account()
    ):
        connector.disconnect()
        raise RuntimeError(
            f"strategy_variant is '{config.strategy_variant}' and execution.mode is not "
            f"'shadow', but the connected MT5 account is not confirmed hedging-mode — "
            f"refusing to start, since a second simultaneous position would just net "
            f"against the first at the broker instead of opening a real, separate ticket."
        )

    engine_cls = STRATEGY_ENGINES.get(config.strategy_variant)
    if engine_cls is None:
        connector.disconnect()
        raise ValueError(
            f"Unknown strategy_variant '{config.strategy_variant}' in config/settings.{args.account}.yaml. "
            f"Valid options: {list(STRATEGY_ENGINES)}"
        )

    kill_switch = KillSwitch(config.kill_switch, args.account)
    executor = TradeExecutor(config.execution, connector, config.symbol)
    engine = engine_cls(config, connector, executor)
    engine.reconcile_on_startup()

    logger.info(
        "Bot started: account=%s symbol=%s timeframe=%s mode=%s strategy_variant=%s state=%s "
        "reject_manual_trades=%s stop_loss_usd=%s breakeven_trigger_usd=%s early_entry_threshold_usd=%s",
        args.account, config.symbol, config.timeframe, config.execution.mode, config.strategy_variant, engine.state.value,
        config.execution.reject_manual_trades, config.stop_loss_usd, config.breakeven_trigger_usd,
        config.early_entry_threshold_usd,
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
                if config.swap_adx_filter is not None:
                    # Only the ADX-gated swap variants read an "adx"
                    # column (see their on_new_candle()) -- every other
                    # variant never touches it, so skip the extra
                    # computation for them. Mirrors scripts/backtest.py's
                    # identical conditional wiring.
                    df = compute_adx(df, period=config.swap_adx_filter.adx_period)

                latest_closed_time = df.iloc[-2].name
                if latest_closed_time != last_closed_candle_time:
                    # Isolated on purpose: on_new_candle() failing must
                    # NEVER prevent on_tick() below from running -- on_tick
                    # is where the stop-loss check lives, and it's the
                    # only thing standing between an open position and an
                    # unbounded loss if something here breaks. Confirmed
                    # 2026-08-21: a missing "adx" column crashed
                    # on_new_candle() every iteration, and because this
                    # used to be one shared try block, on_tick() never ran
                    # again either -- a real position sat unmanaged with
                    # its stop-loss silently disabled until caught by
                    # chance. See docs/STRATEGY_DUAL_CROSS_HISTORY.md's
                    # "CRITICAL INCIDENT" section.
                    try:
                        engine.on_new_candle(df)
                        last_closed_candle_time = latest_closed_time
                    except Exception:
                        logger.exception(
                            "Error in on_new_candle() -- on_tick()/stop-loss check below still "
                            "runs this iteration regardless; will retry candle processing next loop"
                        )

                tick = connector.get_tick(config.symbol)
                engine.on_tick(tick)

                now = time.time()
                if now - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS:
                    session_status = "ACTIVE" if is_within_session(config.sessions[config.strategy_variant]) else "WAITING"
                    account_info = connector.account_info()
                    logger.info(
                        "[HEARTBEAT] state=%s session=%s last_price=%.2f balance=%.2f",
                        engine.state.value, session_status, tick.bid, account_info.balance,
                    )

                    # Status snapshot for api_server.py's gateway — reads
                    # this file instead of opening its own MT5 connection
                    # (which would contend with this one; see SETUP.md).
                    # A write failure here is caught by the outer except
                    # below and just logged; it never interrupts trading.
                    recent_closed_trades = connector.get_recent_closed_trades(
                        config.symbol, config.execution.magic_number,
                    )

                    payload = build_status_payload(
                        account=args.account,
                        bot_state=engine.state.value,
                        session_status=session_status,
                        execution_mode=config.execution.mode,
                        symbol=config.symbol,
                        kill_switch_active=kill_switch.is_active(),
                        account_info={
                            "balance": account_info.balance,
                            "equity": account_info.equity,
                            "profit": account_info.profit,
                            "currency": account_info.currency,
                        },
                        tick={"bid": tick.bid, "ask": tick.ask},
                        open_position=_format_open_position(executor.get_open_position()),
                        recent_closed_trades=recent_closed_trades,
                    )
                    write_status_atomic(status_file_path(config.logging.log_dir, args.account), payload)

                    # Permanent local record for the analytics dashboard —
                    # independent of status.json (which is just the current
                    # snapshot). Dedupes by ticket, so seeing the same
                    # recent trades again next heartbeat is a no-op.
                    append_new_trades(
                        trade_ledger_path(config.logging.log_dir, args.account), recent_closed_trades,
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
