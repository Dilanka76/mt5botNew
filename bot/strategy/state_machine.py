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
import time
from dataclasses import dataclass, field
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

# Grace period after opening a position before we trust a "position not
# found" result as a real close. A live broker can take a moment to make a
# just-filled order visible via positions_get() — without this, that lag
# gets misread as an instant TP fill, the bot loses track of a position
# that's actually still open, and a subsequent cross can open a second,
# genuinely overlapping position. A real TP fill takes minutes to reach the
# $5 target, so this grace period never delays detecting an actual close.
POSITION_CLOSE_GRACE_PERIOD_SECONDS = 5.0


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
    opened_monotonic: float = field(default_factory=time.monotonic)
    stop_loss: float | None = None  # None = no stop-loss configured for this account
    breakeven_armed: bool = False  # set True once price has moved breakeven_trigger_usd in favor


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
        of risking a second, conflicting position. Also runs the
        manual-trade-rejection check, so a stray manual position left open
        from before a restart doesn't linger."""
        self._reject_manual_positions(source="startup")

        position = self.executor.get_open_position()
        if position is None:
            return

        direction = Direction.BUY if position.type == mt5.ORDER_TYPE_BUY else Direction.SELL
        self.open_position = OpenPosition(
            direction=direction,
            ticket=position.ticket,
            entry_price=position.price_open,
            take_profit=position.tp,
            stop_loss=self._compute_stop_loss(direction, position.price_open),
            # A reconciled position has no tick history from this run, so it
            # starts unarmed regardless of its actual unrealized P/L at
            # reconcile time — self-heals if price reaches the trigger again.
            breakeven_armed=False,
        )
        self.state = TradeState.IN_POSITION
        logger.info(
            "Reconciled existing open position on startup: ticket=%s direction=%s entry=%.2f tp=%.2f",
            position.ticket, direction.value, position.price_open, position.tp,
        )

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float | None:
        if self.config.stop_loss_usd is None:
            return None
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def _reject_manual_positions(self, source: str) -> None:
        """If execution.reject_manual_trades is enabled, force-closes any open
        position on this symbol that doesn't carry the bot's own magic number —
        i.e. anything opened by hand in the MT5 terminal GUI (manual orders
        always carry magic=0; MT5's order dialog has no magic field, confirmed
        from live1's real deal history). Never touches the bot's own tracked
        position, which is matched purely by magic and left alone here."""
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
                f"(magic={position.magic}) not opened by this bot, detected via {source}",
                ticket=position.ticket,
                direction="BUY" if position.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=position.volume,
                price=position.price_open,
                magic=position.magic,
                detected_via=source,
            )

    def _active_sessions(self) -> list:
        """Sessions are per strategy_variant (see config/settings.yaml) — this
        looks up the schedule for whichever variant is actually running."""
        return self.config.sessions[self.config.strategy_variant]

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
        if not is_within_session(self._active_sessions()):
            log_decision(
                symbol,
                "cross_ignored_outside_session",
                f"{event.direction.value} cross at {event.candle_time}, no session open",
            )
            return

        gap = calculate_gap(event)
        self._decide_entry(event, gap)

    def _decide_entry(self, event: CrossEvent, gap: float) -> None:
        """Gap-threshold rule: small gap enters immediately, large gap waits
        for an EMA5 touch. Overridden by EMA5OnlyEngine (state_machine_ema5_only.py)
        to always wait for the touch, ignoring the gap entirely."""
        symbol = self.config.symbol
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
        self._reject_manual_positions(source="tick")
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
        if is_within_session(self._active_sessions()):
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
        """Detects a broker-side TP fill (or, in shadow mode, simulates one).

        Skipped for a short grace period right after opening: a live broker
        can take a moment to make a just-filled order visible via
        positions_get(), and without this grace period that lag gets
        misread as an instant TP fill (see POSITION_CLOSE_GRACE_PERIOD_SECONDS)."""
        position = self.open_position

        if time.monotonic() - position.opened_monotonic < POSITION_CLOSE_GRACE_PERIOD_SECONDS:
            return

        # Bot-managed stop-loss: a second, independent exit condition
        # alongside the opposite-cross exit — whichever happens first wins.
        # Never broker-side, so this applies identically in every mode.
        if position.stop_loss is not None:
            stop_hit = (
                (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
            )
            if stop_hit:
                self._close_open_position(reason=f"stop-loss hit at {position.stop_loss:.2f}")
                self.state = TradeState.IDLE
                return

        # Bot-managed breakeven-stop: once armed (price moved
        # breakeven_trigger_usd in favor), a return to the entry price is a
        # second, independent exit condition — checked before take-profit,
        # same reasoning and same "never broker-side" property as the
        # stop-loss check above.
        if self.config.breakeven_trigger_usd is not None:
            sign = 1 if position.direction == Direction.BUY else -1
            favorable = (tick.bid - position.entry_price) * sign
            if not position.breakeven_armed and favorable >= self.config.breakeven_trigger_usd:
                position.breakeven_armed = True
            if position.breakeven_armed and favorable <= 0:
                self._close_open_position(
                    reason=f"breakeven-stop (armed at +{self.config.breakeven_trigger_usd:.2f}, returned to entry)"
                )
                self.state = TradeState.IDLE
                return

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
            stop_loss=self._compute_stop_loss(direction, result.price),
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
