"""The one-position EMA-cross scalping state machine.

Three states:
  IDLE           - no open position, no pending setup, watching for a cross.
  PENDING_ENTRY  - a cross fired with gap >= threshold; waiting for price to
                   touch EMA5 before entering.
  IN_POSITION    - a trade is open; watching for the opposite cross (bot-driven
                   close) or a broker-side TP fill.

Every valid EMA13/21 cross does two things, in this exact order (per spec):
  1. Immediately closes whatever trade is open, regardless of P/L.
  2. Freshly evaluates the new setup in the new direction (gap check, maybe
     wait for EMA5) — never automatic re-entry.

A cross that occurs outside a configured session is ignored entirely: no
setup is created, and the bot simply waits for the next fresh cross once a
session opens (confirmed with the user — crosses aren't "banked" across a
session boundary). If a PENDING setup's EMA5 touch would trigger an entry
after the session has since closed, that entry is skipped too, since the
session rule gates when a trade may actually OPEN, not just when the
triggering cross was detected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import MetaTrader5 as mt5
import pandas as pd

from bot.config import AppConfig
from bot.execution.trade_executor import TradeExecutor
from bot.logging_setup.logger import log_decision
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.sessions import is_within_session
from bot.strategy.cross_detector import CrossEvent, Direction, calculate_gap, detect_cross

logger = logging.getLogger("bot.strategy.state_machine")


class TradeState(Enum):
    IDLE = "IDLE"
    PENDING_ENTRY = "PENDING_ENTRY"
    IN_POSITION = "IN_POSITION"


@dataclass
class PendingSetup:
    direction: Direction
    cross_event: CrossEvent
    gap: float


@dataclass
class OpenPosition:
    direction: Direction
    ticket: int | None  # None in shadow mode
    entry_price: float
    take_profit: float


class EMAScalpEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.pending: PendingSetup | None = None
        self.open_position: OpenPosition | None = None
        self.current_ema5: float | None = None

    def reconcile_on_startup(self) -> None:
        """If a position from a previous run is still open, adopt it instead
        of risking a second, conflicting position."""
        position = self.executor.get_open_position()
        if position is None:
            return

        direction = Direction.BUY if position.type == mt5.ORDER_TYPE_BUY else Direction.SELL
        self.open_position = OpenPosition(
            direction=direction,
            ticket=position.ticket,
            entry_price=position.price_open,
            take_profit=position.tp,
        )
        self.state = TradeState.IN_POSITION
        logger.info(
            "Reconciled existing open position on startup: ticket=%s direction=%s entry=%.2f tp=%.2f",
            position.ticket, direction.value, position.price_open, position.tp,
        )

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> None:
        """Call once per newly closed candle (df's iloc[-2])."""
        self.current_ema5 = float(df_with_emas.iloc[-2]["ema5"])

        event = detect_cross(df_with_emas)
        if event is None:
            return

        symbol = self.config.symbol

        # 1. Close whatever is open, regardless of P/L.
        if self.state == TradeState.IN_POSITION and self.open_position is not None:
            self._close_open_position(reason=f"opposite EMA cross ({event.direction.value})")

        # A cross always invalidates any pending setup (it can only be the
        # opposite direction of whatever we were pending, by definition of
        # what a "cross" is).
        if self.state == TradeState.PENDING_ENTRY and self.pending is not None:
            log_decision(
                symbol,
                "setup_invalidated",
                f"opposite cross before EMA5 touch (was waiting {self.pending.direction.value}, "
                f"gap was {self.pending.gap:.2f})",
            )
            self.pending = None

        self.state = TradeState.IDLE

        # 2. Freshly evaluate the new setup — but only if we're in a session.
        if not is_within_session(self.config.sessions):
            log_decision(
                symbol,
                "cross_ignored_outside_session",
                f"{event.direction.value} cross at {event.candle_time}, no session open",
            )
            return

        gap = calculate_gap(event)
        if gap < self.config.gap_threshold_usd:
            self._enter(event.direction, reason=f"{event.direction.value} cross, gap={gap:.2f} < threshold, immediate entry")
        else:
            self.pending = PendingSetup(direction=event.direction, cross_event=event, gap=gap)
            self.state = TradeState.PENDING_ENTRY
            log_decision(
                symbol,
                "setup_pending",
                f"{event.direction.value} cross, gap={gap:.2f} >= threshold, waiting for EMA5 touch",
            )

    def on_tick(self, tick) -> None:
        """Call frequently (e.g. every ~1s) with the latest tick."""
        if self.state == TradeState.PENDING_ENTRY and self.pending is not None and self.current_ema5 is not None:
            self._check_ema5_touch(tick)
        elif self.state == TradeState.IN_POSITION and self.open_position is not None:
            self._check_position_closed(tick)

    def _check_ema5_touch(self, tick) -> None:
        pending = self.pending
        touched = (
            (pending.direction == Direction.BUY and tick.bid <= self.current_ema5)
            or (pending.direction == Direction.SELL and tick.bid >= self.current_ema5)
        )
        if not touched:
            return

        symbol = self.config.symbol
        if is_within_session(self.config.sessions):
            self._enter(
                pending.direction,
                reason=f"EMA5 touch after {pending.direction.value} cross, gap was {pending.gap:.2f}",
            )
        else:
            log_decision(
                symbol,
                "entry_skipped_outside_session",
                f"EMA5 touch reached for pending {pending.direction.value} setup, but session has closed",
            )
            self.state = TradeState.IDLE

        self.pending = None

    def _check_position_closed(self, tick) -> None:
        """Detects a broker-side TP fill (or, in shadow mode, simulates one)."""
        position = self.open_position

        if self.config.execution.mode == "shadow":
            hit = (
                (position.direction == Direction.BUY and tick.bid >= position.take_profit)
                or (position.direction == Direction.SELL and tick.bid <= position.take_profit)
            )
            if hit:
                log_decision(
                    self.config.symbol,
                    "trade_closed_tp",
                    f"[SHADOW] would have hit TP at {position.take_profit:.2f}",
                )
                self.open_position = None
                self.state = TradeState.IDLE
            return

        if self.executor.get_open_position() is None:
            log_decision(
                self.config.symbol,
                "trade_closed_tp",
                "position no longer open on broker (TP fill or external close)",
            )
            self.open_position = None
            self.state = TradeState.IDLE

    def _enter(self, direction: Direction, reason: str) -> None:
        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)

        result = self.executor.open_market_order(direction, lots, self.config.take_profit_usd)

        self.open_position = OpenPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price, take_profit=result.take_profit,
        )
        self.state = TradeState.IN_POSITION

        log_decision(
            self.config.symbol,
            "trade_entered",
            reason,
            direction=direction.value,
            lots=lots,
            entry=result.price,
            tp=result.take_profit,
            balance=balance,
        )

    def _close_open_position(self, reason: str) -> None:
        position = self.open_position
        self.executor.close_position(position.ticket)
        log_decision(
            self.config.symbol,
            "trade_exited",
            reason,
            direction=position.direction.value,
            entry=position.entry_price,
            ticket=position.ticket,
        )
        self.open_position = None
