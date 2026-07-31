"""Sends orders to MT5. Gated by execution.mode in settings.yaml.

mode=shadow (default): nothing is ever sent to the broker, only logged.
This is what should be used for all initial strategy testing.
"""
from __future__ import annotations

import logging

import MetaTrader5 as mt5

from bot.config import ExecutionConfig
from bot.mt5_connector import MT5Connector
from bot.strategy.base import Direction, Signal

logger = logging.getLogger("bot.execution")


class ExecutionError(Exception):
    pass


class TradeExecutor:
    def __init__(self, config: ExecutionConfig, connector: MT5Connector):
        self.config = config
        self.connector = connector

    def execute(self, signal: Signal, lot: float, stop_loss: float, take_profit: float) -> dict:
        if self.config.mode == "shadow":
            logger.info(
                "[SHADOW] Would place %s %s lot=%.2f entry=%.5f sl=%.5f tp=%.5f reason=%s",
                signal.direction.value,
                signal.symbol,
                lot,
                signal.entry_price,
                stop_loss,
                take_profit,
                signal.reason,
            )
            return {"status": "shadow"}

        if self.config.require_demo_account and not self.connector.is_demo_account():
            raise ExecutionError(
                "require_demo_account is true but the connected account is not a demo account. "
                "Refusing to place order."
            )

        self.connector.ensure_symbol(signal.symbol)
        tick = mt5.symbol_info_tick(signal.symbol)
        if tick is None:
            raise ExecutionError(f"No tick data for {signal.symbol}")

        order_type = mt5.ORDER_TYPE_BUY if signal.direction == Direction.BUY else mt5.ORDER_TYPE_SELL
        price = tick.ask if signal.direction == Direction.BUY else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": signal.symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": self.config.order_deviation_points,
            "magic": self.config.magic_number,
            "comment": self.config.order_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise ExecutionError(f"order_send failed: {result}")

        logger.info(
            "Order placed: %s %s lot=%.2f price=%.5f sl=%.5f tp=%.5f ticket=%s",
            signal.direction.value,
            signal.symbol,
            lot,
            price,
            stop_loss,
            take_profit,
            result.order,
        )
        return {"status": "executed", "ticket": result.order, "result": result}
