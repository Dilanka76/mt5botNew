"""The cross-confirmed-adaptive-TP state machine — strategy_variant=
cross_confirmed_adaptive_tp.

Identical entry mechanism to cross_confirmed (state_machine_cross_confirmed.py):
entry ONLY on a genuinely confirmed close-based EMA13/21 cross, at most one
position at a time, auto-replaces on a fresh confirmed opposite cross. The
only difference is how the take-profit distance is computed for each entry.

Requested formula: tp_distance = take_profit_usd - (candle_close - candle_open)
of the CONFIRMING candle (the same closed candle whose cross triggered this
entry) — a literal signed value, not absolute. A candle that already closed
higher than it opened (positive close-open) shrinks the TP below the base;
one that closed lower than it opened (negative close-open) grows the TP
above the base.

That formula can go to zero or negative if the confirming candle's own
close-open move is >= take_profit_usd in the same direction — a take-profit
distance can never be zero/negative (it would sit at or behind the entry
price), so it's floored at MIN_TP_DISTANCE_USD and every time the floor is
hit it's logged explicitly via log_decision (category "tp_floor_applied")
so it's visible in the results rather than silently placing a degenerate
order.
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

logger = logging.getLogger("bot.strategy.state_machine_cross_confirmed_adaptive_tp")

MIN_TP_DISTANCE_USD = 0.5


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


class CrossConfirmedAdaptiveTPEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=cross_confirmed_adaptive_tp requires stop_loss_usd to be "
                "set — same mandatory $stop_loss_usd backstop as cross_confirmed."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: OpenPosition | None = None
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["cross_confirmed_adaptive_tp"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def _compute_tp_distance(self, candle_open: float, candle_close: float) -> float:
        candle_range = candle_close - candle_open
        raw_distance = self.config.take_profit_usd - candle_range
        if raw_distance < MIN_TP_DISTANCE_USD:
            log_decision(
                self.config.symbol, "tp_floor_applied",
                f"adaptive TP formula gave {raw_distance:.2f} (base={self.config.take_profit_usd:.2f}, "
                f"candle_range={candle_range:.2f}) — floored to {MIN_TP_DISTANCE_USD:.2f}",
                base=self.config.take_profit_usd, candle_range=candle_range, floored_to=MIN_TP_DISTANCE_USD,
            )
            return MIN_TP_DISTANCE_USD
        return raw_distance

    def _reject_manual_positions(self) -> None:
        if not self.config.execution.reject_manual_trades:
            return
        shadow = self.config.execution.mode == "shadow"
        for position in self.executor.get_all_positions():
            if position.magic == self.config.execution.magic_number:
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
        open_price = float(last_closed["open"])
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
                            direction, close_price, open_price, last_closed_time,
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

    def _enter(self, direction: Direction, price: float, open_price: float, candle_time, reason: str) -> OpenedTrade | None:
        tp_distance = self._compute_tp_distance(open_price, price)
        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)
        result = self.executor.open_market_order(direction, lots, tp_distance)

        self.position = OpenPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self._compute_stop_loss(direction, result.price),
        )

        log_decision(
            self.config.symbol, "trade_entered", reason,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            tp_distance=tp_distance, stop_loss=self.position.stop_loss, balance=balance,
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
