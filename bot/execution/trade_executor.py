"""Sends orders to MT5. Gated by execution.mode in settings.yaml.

mode=shadow (default): no real order is ever sent to the broker. open_market_order
still returns a synthetic OrderResult built from the live tick price, so the
state machine's logic (entry price, TP tracking) runs identically in shadow
and demo_execute/live_execute — the only difference is whether mt5.order_send
is actually called.

Per the strategy spec, there is no stop-loss field: the only exits are the
take-profit (set on the order) and a bot-driven close on an opposite EMA
cross (close_position).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import MetaTrader5 as mt5

from bot.config import ExecutionConfig
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import Direction

logger = logging.getLogger("bot.execution")

# Real incident, 2026-08-27: demo2's order_comment ("dual-cross-confirmed-swap",
# 25 chars) plus the "-close" suffix below hit exactly 31 characters, and MT5
# rejected every close_position() call with retcode=None, last_error=(-2,
# 'Invalid "comment" argument') -- the documented 31-char MT5 comment limit is
# not safe to rely on exactly at the boundary. 26 leaves real margin and still
# fits every comment used in this project so far.
MAX_ORDER_COMMENT_LENGTH = 26


def _safe_comment(comment: str, *, suffix: str = "") -> str:
    """Truncates the BASE comment first so a suffix (e.g. "-close") always
    survives intact rather than being cut off itself."""
    base_budget = MAX_ORDER_COMMENT_LENGTH - len(suffix)
    return comment[:base_budget] + suffix


class ExecutionError(Exception):
    pass


@dataclass
class OrderResult:
    ticket: int | None  # None in shadow mode (no real order exists)
    price: float
    take_profit: float


class TradeExecutor:
    def __init__(self, config: ExecutionConfig, connector: MT5Connector, symbol: str):
        self.config = config
        self.connector = connector
        self.symbol = symbol

    def get_open_position(self):
        """Returns this bot's open position (matched by symbol + magic number), or None.
        Returns only the FIRST match — fine for every single-position engine
        (gap_threshold), but silently discards a second magic-matched
        position if one ever exists. dual_cross (up to 2 simultaneous
        positions) must use get_open_positions() below instead."""
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return None
        for position in positions:
            if position.magic == self.config.magic_number:
                return position
        return None

    def get_open_positions(self) -> list:
        """Every one of THIS bot's open positions (matched by symbol + magic
        number) — unlike get_open_position(), does not stop at the first
        match. Used by strategy_variant=dual_cross, which can legitimately
        hold up to 2 simultaneous opposite-direction positions."""
        positions = mt5.positions_get(symbol=self.symbol)
        return [p for p in (positions or []) if p.magic == self.config.magic_number]

    def get_all_positions(self):
        """Every open position for this symbol, unfiltered by magic number —
        used by the manual-trade-rejection safety check to see positions
        get_open_position() deliberately filters out."""
        return list(mt5.positions_get(symbol=self.symbol) or [])

    def open_market_order(self, direction: Direction, lots: float, take_profit_distance: float) -> OrderResult:
        tick = self.connector.get_tick(self.symbol)
        price = tick.ask if direction == Direction.BUY else tick.bid
        take_profit = price + take_profit_distance if direction == Direction.BUY else price - take_profit_distance

        if self.config.mode == "shadow":
            logger.info(
                "[SHADOW] Would open %s %s lots=%.2f price=%.2f tp=%.2f",
                direction.value, self.symbol, lots, price, take_profit,
            )
            return OrderResult(ticket=None, price=price, take_profit=take_profit)

        if self.config.require_demo_account and not self.connector.is_demo_account():
            raise ExecutionError(
                "require_demo_account is true but the connected account is not a demo account. "
                "Refusing to place order."
            )

        self.connector.ensure_symbol(self.symbol)
        order_type = mt5.ORDER_TYPE_BUY if direction == Direction.BUY else mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "tp": take_profit,
            "deviation": self.config.order_deviation_points,
            "magic": self.config.magic_number,
            "comment": _safe_comment(self.config.order_comment),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            raise ExecutionError(
                f"order_send (open) failed: {result}; mt5.last_error()={mt5.last_error()}"
            )

        logger.info(
            "Order opened: %s %s lots=%.2f price=%.2f tp=%.2f ticket=%s",
            direction.value, self.symbol, lots, price, take_profit, result.order,
        )
        return OrderResult(ticket=result.order, price=price, take_profit=take_profit)

    def set_sltp(self, ticket: int | None, stop_loss: float | None, take_profit: float | None) -> bool:
        """Sets a REAL broker-side stop-loss and/or take-profit on an open
        position. Pass 0.0 to clear one; None to leave it unchanged.

        Until this was added (2026-09-07) nothing in this project ever
        placed a broker-side stop -- open_market_order sends only "tp", and
        every stop-loss is enforced in software by the engine's polling
        loop, which closes at market when it notices the level was passed.
        That is fine while the bot is alive and worthless if it is not.

        The TP-runner rule needs this: it removes the broker take-profit so
        a winner can keep running, which would otherwise leave the position
        with NO broker-side order at all -- no target and no stop -- for as
        long as it runs. Setting a real stop at the locked profit means the
        broker closes the trade in profit even if the bot dies, which is
        strictly safer than how every trade behaves today.

        Returns True on success. A failure here is NOT raised: the software
        stop is still running and still protects the position, so a
        rejected modify must not take down the trading loop. It is logged
        loudly instead.
        """
        if self.config.mode == "shadow":
            logger.info("[SHADOW] Would set ticket=%s sl=%s tp=%s", ticket, stop_loss, take_profit)
            return True

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning("set_sltp: ticket %s not found (already closed?)", ticket)
            return False
        position = positions[0]

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": ticket,
            "sl": position.sl if stop_loss is None else stop_loss,
            "tp": position.tp if take_profit is None else take_profit,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "set_sltp failed (software stop still active): ticket=%s sl=%s tp=%s result=%s "
                "mt5.last_error()=%s",
                ticket, stop_loss, take_profit, result, mt5.last_error(),
            )
            return False

        logger.info("set_sltp: ticket=%s sl=%s tp=%s", ticket, request["sl"], request["tp"])
        return True

    def close_position(self, ticket: int | None) -> None:
        """Force-closes the position at market. Used for the opposite-EMA-cross exit."""
        if self.config.mode == "shadow":
            logger.info("[SHADOW] Would close ticket=%s", ticket)
            return

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning("close_position: ticket %s not found (already closed?)", ticket)
            return
        position = positions[0]

        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = self.connector.get_tick(position.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": self.config.order_deviation_points,
            "magic": self.config.magic_number,
            "comment": _safe_comment(self.config.order_comment, suffix="-close"),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            # result is None (not even a rejection code) whenever order_send
            # couldn't submit the request at all -- mt5.last_error() carries
            # the actual reason in that case (e.g. AutoTrading disabled,
            # invalid request) and is otherwise silently lost, which cost
            # real debugging time tracing a manual-trade-rejection failure
            # back to AutoTrading being off (2026-08-27).
            raise ExecutionError(
                f"order_send (close) failed: {result}; mt5.last_error()={mt5.last_error()}"
            )

        logger.info("Position closed: ticket=%s price=%.2f", ticket, price)
