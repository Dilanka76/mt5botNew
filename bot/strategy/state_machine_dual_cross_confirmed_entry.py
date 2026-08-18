"""The dual-cross-confirmed-entry state machine — strategy_variant=
dual_cross_confirmed_entry.

Built on user request (2026-08-18), sitting alongside dual_cross (see
state_machine_dual_cross.py) without changing it. Two mechanisms are
deliberately inverted relative to dual_cross:

Entry: NO tick-based tolerance entry at all. A position only ever opens at
a candle's close, when the real, close-based EMA13/EMA21 shows a genuine,
confirmed flip. This guarantees every real cross produces a trade — there
is no tick-timing/tolerance window for a genuine cross to slip through
unentered (the gap dual_cross's §4b fallback exists to catch — see that
engine's module docstring for the real-world case this was built from).

Closing the previous (opposite) position: whenever a confirmed cross
happens and an opposite-direction position is still open, that position
gets closed — same triggering condition as dual_cross's §5, but with a
different priority for HOW it closes. This engine tries fast first: on
every tick, if the opposite direction's provisional EMA13/21 comes within
closing_tolerance_usd of a genuine flip, that position closes right then,
before the candle has even finished forming. Only if no tick ever
satisfies that during the candle does it fall back to closing the normal
way, at the candle's actual close.

IMPORTANT structural consequence of that rule, confirmed with the user
before building this: because the opposite position ALWAYS closes
whenever a new direction confirms (never conditionally, the way
dual_cross defers to the new position's own validation), this engine can
never hold two positions at once — unlike dual_cross's genuine
concurrency, this is a single-position, auto-replacing design (closer in
shape to cross_confirmed than to dual_cross), just with a smarter/faster
closing trigger than cross_confirmed's plain "close immediately when a
new cross confirms". There can be a brief gap where nothing is open (the
old one closed early via tick-tolerance, the new one hasn't confirmed
yet), but never true overlap.

Reuses OpenedTrade/ClosedTrade from state_machine_dual_cross.py so
bot/backtest/runner.py's existing event-consuming recording path works
for this engine unchanged too. Reuses OpenPosition (not DualPosition —
no concurrency-specific fields needed here) from state_machine.py, same
as cross_confirmed does.
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
from bot.strategy.state_machine import POSITION_CLOSE_GRACE_PERIOD_SECONDS, OpenPosition, TradeState
from bot.strategy.state_machine_dual_cross import ClosedTrade, OpenedTrade

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_confirmed_entry")


def _opposite(direction: Direction) -> Direction:
    return Direction.SELL if direction == Direction.BUY else Direction.BUY


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


class DualCrossConfirmedEntryEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_entry requires stop_loss_usd to be set."
            )
        if config.dual_cross_confirmed_entry is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_entry requires a "
                "'dual_cross_confirmed_entry:' config section."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: OpenPosition | None = None
        # The previous CLOSED candle's real EMA13/21 — baseline for every
        # tick's provisional calculation (closing check) and for detecting
        # a genuine flip at each candle's own close (entry check).
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_confirmed_entry"]

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

    def on_new_candle(self, df_with_emas) -> list[OpenedTrade | ClosedTrade]:
        """Two jobs, in order: (1) fallback-close the open position if this
        candle's real close confirms a reversal against it (the tick-based
        closing check in on_tick is the primary path — this only fires if
        that never triggered during the candle's own formation), (2) the
        ONLY entry trigger this engine has — a genuine confirmed cross
        with no position already open in that direction."""
        events: list[OpenedTrade | ClosedTrade] = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        close_price = float(last_closed["close"])

        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if is_confirmed_cross:
                direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL

                if self.position is not None and self.position.direction == direction:
                    pass  # same-direction reconfirm — nothing to do
                else:
                    # (1) Fallback close: only reached if the tick-based
                    # tolerance check (on_tick) never caught this reversal
                    # during the candle's own formation.
                    if self.position is not None:
                        events.append(self._close_position(
                            category="closed_confirmed_reversal",
                            reason=(
                                f"{direction.value} cross confirmed at candle close (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f}) -> closing the opposite {self.position.direction.value} "
                                f"now (tick-based tolerance close never triggered this candle)"
                            ),
                            exit_price=close_price,
                        ))

                    # (2) Entry — the only trigger in this design.
                    if is_within_session(self._active_sessions()):
                        opened = self._enter(
                            direction, close_price, last_closed_time,
                            reason=(
                                f"{direction.value} cross confirmed at candle close "
                                f"(ema13={ema13:.2f}, ema21={ema21:.2f})"
                            ),
                        )
                        if opened is not None:
                            events.append(opened)
                    else:
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} confirmed cross at {last_closed_time}, no session open",
                        )

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self._update_state()
        return events

    def on_tick(self, tick) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
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

        # Tick-based closing-tolerance check — the PRIMARY way the open
        # position closes when the opposite direction is developing.
        # Watches the tick-level provisional EMA13/21: if it has flipped
        # to the OPPOSITE of the open position's own direction and is
        # within closing_tolerance_usd, close it now, before candle close.
        if self.position is not None and self.prev_ema13 is not None and self.prev_ema21 is not None:
            k_mid = 2 / (self.config.ema_periods.mid + 1)
            k_slow = 2 / (self.config.ema_periods.slow + 1)
            prov13 = tick.bid * k_mid + self.prev_ema13 * (1 - k_mid)
            prov21 = tick.bid * k_slow + self.prev_ema21 * (1 - k_slow)

            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            prov_state = _classify(prov13, prov21)
            is_flip = prev_state is not None and prov_state is not None and prev_state != prov_state
            within_tolerance = abs(prov13 - prov21) <= self.config.dual_cross_confirmed_entry.closing_tolerance_usd

            if is_flip and within_tolerance:
                incoming_direction = Direction.BUY if prov_state == CrossState.ABOVE else Direction.SELL
                if self.position is not None and self.position.direction == _opposite(incoming_direction):
                    events.append(self._close_position(
                        category="closed_tick_tolerance",
                        reason=(
                            f"tick-based close: provisional EMA13/21 within "
                            f"${self.config.dual_cross_confirmed_entry.closing_tolerance_usd:.2f} "
                            f"(actual ${abs(prov13 - prov21):.2f}) of flipping to {incoming_direction.value} "
                            f"-> closing the opposite {self.position.direction.value} now, before candle close"
                        ),
                        exit_price=tick.bid,
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
        """Bot-initiated close (stop_loss, closed_tick_tolerance,
        closed_confirmed_reversal) — actively places a real close order
        via the executor."""
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
        """The position is already gone at the broker (a real TP fill) or
        is being simulated as such (shadow mode) — no executor.close_position()
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
