"""The cross-confirmed state machine — strategy_variant=cross_confirmed.

Built for a specific backtest comparison against dual_cross: dual_cross
found that 83.3% of its losses came from entering on a live tick near the
provisional EMA13/21 equal point, only for the candle's own close to show
the cross never actually confirmed (see decisions/analysis this variant
was requested to test against). This engine removes that entirely — there
is NO tick-based tolerance entry at all. The ONLY entry trigger is a
genuinely confirmed cross: the real, close-based EMA13/EMA21 relationship
actually flipping versus the previous closed candle. Entry happens right
at that candle's own close price.

Because entry now only ever happens on an already-confirmed cross, there
is no more "provisional, not yet certain" gap for a second position to
open into — so unlike dual_cross, this engine holds AT MOST ONE position
at a time. A fresh confirmed opposite cross auto-replaces (closes then
opens), the same shape gap_threshold's old "immediate entry" auto-close
used, category "new_cross_confirmed".

Kept identical to dual_cross: the mandatory $stop_loss_usd stop-loss and
$take_profit_usd take-profit, both checked every tick per open position;
session gating on new entries; reuses OpenedTrade/ClosedTrade from
state_machine_dual_cross.py so bot/backtest/runner.py's event-consuming
recording works unchanged for both engines. is_concurrent_entry is always
False here (concurrency doesn't exist in this design) and
cross_candle_time is stamped as the confirming candle's own timestamp,
purely for interface-shape compatibility with dual_cross's event types.
"""
from __future__ import annotations

import logging
import time

import MetaTrader5 as mt5

from bot.config import AppConfig
from bot.execution.trade_executor import TradeExecutor
from bot.logging_setup.logger import log_decision
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.sessions import is_within_session
from bot.strategy.cross_detector import CrossState, Direction
from bot.strategy.state_machine import (
    POSITION_CLOSE_GRACE_PERIOD_SECONDS,
    OpenPosition,
    TradeState,
)
from bot.strategy.state_machine_dual_cross import ClosedTrade, OpenedTrade

logger = logging.getLogger("bot.strategy.state_machine_cross_confirmed")


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


class CrossConfirmedEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=cross_confirmed requires stop_loss_usd to be set — same "
                "mandatory $stop_loss_usd backstop as dual_cross, not optional here either."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: OpenPosition | None = None
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["cross_confirmed"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def _reject_manual_positions(self) -> None:
        if not self.config.execution.reject_manual_trades:
            return
        shadow = self.config.execution.mode == "shadow"
        for position in self.executor.get_all_positions():
            if position.magic == self.config.execution.magic_number or position.magic in self.config.execution.sibling_magic_numbers:
                continue
            try:
                self.executor.close_position(position.ticket)
            except Exception:
                logger.exception(
                    "Failed to close manual/foreign position ticket=%s — will retry next tick",
                    position.ticket,
                )
                continue
            log_decision(
                self.config.symbol,
                "manual_trade_rejected",
                f"{'[SHADOW] would close' if shadow else 'closed'} foreign position "
                f"(magic={position.magic}) not opened by this bot",
                ticket=position.ticket,
                direction="BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=position.volume,
                price=position.price_open,
                magic=position.magic,
            )

    def reconcile_on_startup(self) -> None:
        self._reject_manual_positions()
        broker_position = self.executor.get_open_position()
        if broker_position is not None:
            direction = Direction.BUY if broker_position.type == mt5.ORDER_TYPE_BUY else Direction.SELL
            self.position = OpenPosition(
                direction=direction,
                ticket=broker_position.ticket,
                entry_price=broker_position.price_open,
                take_profit=broker_position.tp,
                stop_loss=self._compute_stop_loss(direction, broker_position.price_open),
            )
            log_decision(
                self.config.symbol, "position_reconciled",
                f"Adopted existing {direction.value} position on startup",
                ticket=broker_position.ticket, entry=broker_position.price_open, tp=broker_position.tp,
            )
        self._update_state()

    def _update_state(self) -> None:
        self.state = TradeState.IN_POSITION if self.position is not None else TradeState.IDLE

    def on_new_candle(self, df_with_emas) -> list:
        """The only entry trigger in this design: a genuine, confirmed
        cross at candle close. No tick-based check exists — see module
        docstring."""
        events: list = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        close_price = float(last_closed["close"])

        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            curr_state = _classify(ema13, ema21)
            is_confirmed_cross = (
                prev_state is not None and curr_state is not None and prev_state != curr_state
            )
            if is_confirmed_cross:
                direction = Direction.BUY if curr_state == CrossState.ABOVE else Direction.SELL
                if self.position is not None and self.position.direction == direction:
                    pass  # same-direction reconfirm — nothing to do
                else:
                    if self.position is not None:
                        events.append(self._close_position(
                            category="new_cross_confirmed",
                            reason=(
                                f"new confirmed {direction.value} cross took over "
                                f"(was {self.position.direction.value})"
                            ),
                            exit_price=close_price,
                        ))
                    if is_within_session(self._active_sessions()):
                        opened = self._enter(
                            direction, close_price, last_closed_time,
                            reason=f"{direction.value} cross confirmed at candle close (ema13={ema13:.2f}, ema21={ema21:.2f})",
                        )
                        if opened is not None:
                            events.append(opened)
                    else:
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} cross confirmed at {last_closed_time}, no session open",
                        )

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self._update_state()
        return events

    def on_tick(self, tick) -> list:
        events: list = []
        self._reject_manual_positions()

        if self.position is not None:
            position = self.position
            if time.monotonic() - position.opened_monotonic >= POSITION_CLOSE_GRACE_PERIOD_SECONDS:
                stop_hit = (
                    (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                    or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
                )
                if stop_hit:
                    events.append(self._close_position(
                        category="stop_loss",
                        reason=f"${self.config.stop_loss_usd:.2f} stop-loss hit at {position.stop_loss:.2f}",
                        exit_price=position.stop_loss,
                    ))
                else:
                    if self.config.execution.mode == "shadow":
                        tp_hit = (
                            (position.direction == Direction.BUY and tick.bid >= position.take_profit)
                            or (position.direction == Direction.SELL and tick.bid <= position.take_profit)
                        )
                        if tp_hit:
                            events.append(self._record_broker_closed(
                                reason=f"[SHADOW] would have hit TP at {position.take_profit:.2f}",
                                exit_price=position.take_profit,
                            ))
                    else:
                        if self.executor.get_open_position() is None:
                            events.append(self._record_broker_closed(
                                reason="position no longer open on broker (TP fill or external close)",
                                exit_price=position.take_profit,
                            ))

        self._update_state()
        return events

    def _enter(self, direction: Direction, price: float, candle_time, reason: str) -> OpenedTrade | None:
        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)
        result = self.executor.open_market_order(direction, lots, self.config.take_profit_usd)

        self.position = OpenPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self._compute_stop_loss(direction, result.price),
        )

        log_decision(
            self.config.symbol, "trade_entered", reason,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            stop_loss=self.position.stop_loss, balance=balance,
        )

        return OpenedTrade(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self.position.stop_loss,
            cross_candle_time=candle_time, is_concurrent_entry=False,
        )

    def _close_position(self, category: str, reason: str, exit_price: float) -> ClosedTrade:
        """Bot-initiated close (stop_loss, new_cross_confirmed) — actively
        places a real close order via the executor."""
        position = self.position
        self.position = None
        self.executor.close_position(position.ticket)
        log_decision(
            self.config.symbol, "trade_exited", reason,
            direction=position.direction.value, entry=position.entry_price, ticket=position.ticket, category=category,
        )
        return ClosedTrade(
            direction=position.direction, ticket=position.ticket, entry_price=position.entry_price,
            exit_price=exit_price, category=category, reason=reason,
        )

    def _record_broker_closed(self, reason: str, exit_price: float) -> ClosedTrade:
        """The position is already gone at the broker (real TP fill) or is
        being simulated as such (shadow mode) — no executor.close_position()
        call, there's nothing left to close."""
        position = self.position
        self.position = None
        log_decision(
            self.config.symbol, "trade_closed_tp", reason,
            direction=position.direction.value, ticket=position.ticket,
        )
        return ClosedTrade(
            direction=position.direction, ticket=position.ticket, entry_price=position.entry_price,
            exit_price=exit_price, category="take_profit", reason=reason,
        )
