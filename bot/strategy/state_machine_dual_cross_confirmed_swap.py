"""dual_cross_confirmed_swap ("confirmed-entry-only + immediate swap, no
debounce, no ADX gate, no stop-tightening") — strategy_variant=
dual_cross_confirmed_swap.

Built 2026-08-27 for a brand-new account family (demo2_m1/demo2_m3),
managed independently from demo1_m1/demo1_m3's dual_cross_confirmed_swap_adx
(see bot/strategy/state_machine_dual_cross_confirmed_swap_adx.py). User's
explicit spec, given directly (not derived from real-trade forensics like
the demo1 lineage was): confirmed-cross entry, gap+EMA5-pullback rule
unchanged, take-profit/stop-loss are the only fixed exits, and any genuine
confirmed opposite cross flips the position IMMEDIATELY — no 2-candle
debounce, no ADX(14) gate, no pending-reversal stop-loss tightening. All
three of those mechanisms exist in the demo1 lineage (built from demo1's own
real-trade whipsaw losses) but were explicitly rejected for this account:
"no need this, 2 confirmation candle, and the adx no need there" /
"this part no need" (referring to the stop-tightening).

Per-account parameters differ between the two legs (both set via plain
config, not hardcoded here): demo2_m1 = stop_loss_usd 5.0 / take_profit_usd
5.0 / gap_threshold_usd 5.0; demo2_m3 = stop_loss_usd 10.0 / take_profit_usd
6.0 / gap_threshold_usd 7.0.

Structurally this is dual_cross_confirmed_swap_adx with three things
removed:
  - No swap_adx_filter — not read, not required by the constructor.
  - No 2-candle debounce (self.pending_reversal_direction doesn't exist
    here) — the very first candle whose close confirms an opposite
    EMA13/21 cross closes the held position and opens the new one, same
    instant.
  - No stop-loss tightening — position.stop_loss is always the full
    config.stop_loss_usd distance from entry, for the position's entire
    life (until a swap or TP/stop closes it).

Everything else is identical to dual_cross_confirmed_swap_adx: confirmed-
close-only entry (no tick-based tolerance path at all), the $X gap +
EMA5-pullback rule on FLAT entries only (never on the swap's own re-entry,
same reasoning as the demo1 lineage — gating the swap's re-entry risks
leaving the bot flat through the very reversal it exists to catch), every
position opens already validated (no unvalidated state, no
validation_failed category), and the stop-loss is active on every position
from the instant it opens (no early-exit net).

New closed-trade category name: swapped_reversal (deliberately NOT
swapped_confirmed_reversal — that name in the demo1 lineage specifically
means "confirmed across 2 candles + ADX," which is not what happens here;
reusing it would mislead any future analysis script that assumes that
semantics).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import MetaTrader5 as mt5
import pandas as pd

from bot.config import AppConfig
from bot.execution.trade_executor import TradeExecutor
from bot.logging_setup.logger import log_decision
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.sessions import is_within_session
from bot.strategy.cross_detector import CrossState, Direction
from bot.strategy.state_machine import POSITION_CLOSE_GRACE_PERIOD_SECONDS, TradeState
from bot.strategy.state_machine_dual_cross import ClosedTrade, DualPosition, OpenedTrade

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_confirmed_swap")


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


@dataclass
class PendingSetup:
    """A flat-entry setup whose gap was too wide to enter immediately —
    waiting for a pullback to EMA5. Only ever exists while self.position
    is None (see module docstring — never interacts with the swap path)."""
    direction: Direction
    reason: str
    cross_candle_time: pd.Timestamp | None
    gap: float


class DualCrossConfirmedSwapEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap requires stop_loss_usd to be set."
            )
        if config.gap_threshold_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap requires gap_threshold_usd to be set "
                "(the gap + EMA5-pullback rule on flat entries)."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: DualPosition | None = None
        self.pending: PendingSetup | None = None
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None
        self.current_ema5: float | None = None
        self.current_candle_time: pd.Timestamp | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_confirmed_swap"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        distance = self.config.stop_loss_usd
        return (
            entry_price - distance if direction == Direction.BUY
            else entry_price + distance
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
                self.config.symbol, "manual_trade_rejected",
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
            self.position = DualPosition(
                direction=direction, ticket=broker_position.ticket, entry_price=broker_position.price_open,
                take_profit=broker_position.tp, stop_loss=self._compute_stop_loss(direction, broker_position.price_open),
                cross_candle_time=None, is_concurrent_entry=False, validated=True,
            )
            log_decision(
                self.config.symbol, "position_reconciled",
                f"Adopted existing {direction.value} position on startup (self-healed)",
                ticket=broker_position.ticket, entry=broker_position.price_open, tp=broker_position.tp,
            )
        self._update_state()

    def _update_state(self) -> None:
        self.state = TradeState.IN_POSITION if self.position is not None else TradeState.IDLE

    def _maybe_enter_or_pend(
        self, direction: Direction, price_now: float, ema13_now: float, base_reason: str,
        cross_candle_time_override: pd.Timestamp | None,
    ) -> OpenedTrade | None:
        """Flat-entry-only gap check (see module docstring) — NEVER called
        from the swap path, which always enters immediately via _enter()
        directly."""
        gap = abs(price_now - ema13_now)
        if gap < self.config.gap_threshold_usd:
            return self._enter(
                direction,
                reason=f"{base_reason}, gap={gap:.2f} < ${self.config.gap_threshold_usd:.2f} threshold -> immediate entry",
                cross_candle_time_override=cross_candle_time_override,
            )
        else:
            self.pending = PendingSetup(
                direction=direction,
                reason=f"{base_reason}, gap={gap:.2f} >= ${self.config.gap_threshold_usd:.2f} threshold",
                cross_candle_time=cross_candle_time_override,
                gap=gap,
            )
            log_decision(
                self.config.symbol, "setup_pending",
                f"{direction.value} {self.pending.reason} -> waiting for EMA5 touch",
            )
            return None

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema5 = float(last_closed["ema5"])
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        exit_price = float(last_closed["close"])

        # No own-candle-validation step here at all — every position opens
        # already validated (see module docstring), so there is nothing to
        # check on the candle following entry.

        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if self.position is not None:
                held_state = CrossState.ABOVE if self.position.direction == Direction.BUY else CrossState.BELOW
                if new_state is not None and new_state != held_state:
                    # Immediate swap — no debounce, no ADX gate (see module
                    # docstring). The very first candle whose close
                    # confirms an opposite cross flips the position right
                    # here, regardless of P/L.
                    direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL
                    events.append(self._close_position(
                        category="swapped_reversal",
                        reason=(
                            f"{direction.value} cross confirmed at candle close (ema13={ema13:.2f}, "
                            f"ema21={ema21:.2f}) -> closing the opposite {self.position.direction.value} "
                            f"now, regardless of P/L"
                        ),
                        exit_price=exit_price,
                    ))
                    if not is_within_session(self._active_sessions()):
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} confirmed cross at {last_closed_time}, no session open",
                        )
                    else:
                        opened = self._enter(
                            direction,
                            reason=(
                                f"close-confirmed (immediate reversal): candle closed with a genuine "
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                        )
                        if opened is not None:
                            events.append(opened)
            else:
                # Flat -> the ONLY entry path this engine has: a genuine,
                # already-closed-candle EMA13/21 cross, gated by the gap +
                # EMA5-pullback rule (see module docstring). No tick-based
                # tolerance path exists at all in this engine.
                direction = (Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL) if is_confirmed_cross else None
                if is_confirmed_cross:
                    # Rule 1: a genuine opposite confirmed cross cancels
                    # any still-pending setup outright, before considering
                    # this new cross at all.
                    if self.pending is not None and self.pending.direction != direction:
                        log_decision(
                            self.config.symbol, "pending_cancelled",
                            f"{direction.value} cross confirmed at candle close -> cancelling pending "
                            f"{self.pending.direction.value} setup (EMA5 never touched, gap was {self.pending.gap:.2f})",
                        )
                        self.pending = None

                    if not is_within_session(self._active_sessions()):
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} confirmed cross at {last_closed_time}, no session open",
                        )
                    else:
                        opened = self._maybe_enter_or_pend(
                            direction, exit_price, ema13,
                            base_reason=(
                                f"close-confirmed: candle closed with a genuine "
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                        )
                        if opened is not None:
                            events.append(opened)

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self.current_ema5 = ema5
        self.current_candle_time = df_with_emas.index[-1]
        self._update_state()
        return events

    def _check_ema5_touch(self, tick) -> OpenedTrade | None:
        """The only entry-related check on_tick() does: has price pulled
        back to touch EMA5 for a still-pending flat-entry setup? Never
        interacts with the swap path (self.pending only ever exists while
        self.position is None — see PendingSetup's docstring)."""
        if self.pending is None or self.position is not None or self.current_ema5 is None:
            return None
        pending = self.pending
        touched = (
            (pending.direction == Direction.BUY and tick.bid <= self.current_ema5)
            or (pending.direction == Direction.SELL and tick.bid >= self.current_ema5)
        )
        if not touched:
            return None
        if not is_within_session(self._active_sessions()):
            log_decision(
                self.config.symbol, "pending_touch_outside_session",
                f"EMA5 touch reached for pending {pending.direction.value} setup, but no session open",
            )
            self.pending = None
            return None
        opened = self._enter(
            pending.direction,
            reason=f"EMA5 touch at {tick.bid:.2f} for pending {pending.direction.value} setup ({pending.reason})",
            cross_candle_time_override=pending.cross_candle_time,
        )
        self.pending = None
        return opened

    def on_tick(self, tick) -> list[OpenedTrade | ClosedTrade]:
        # The only entry logic here is the EMA5-touch check for a pending
        # flat-entry setup below — everything else (fresh close-confirmed
        # entries, swap re-entries) happens in on_new_candle(). Otherwise
        # this just checks an EXISTING position for the stop-loss or a
        # broker-side TP/close, every tick.
        events: list[OpenedTrade | ClosedTrade] = []
        self._reject_manual_positions()

        live_tickets: set | None = None
        if self.config.execution.mode != "shadow" and self.position is not None:
            live_tickets = {p.ticket for p in self.executor.get_open_positions()}

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
                        if position.ticket not in live_tickets:
                            events.append(self._record_broker_closed(
                                reason="position no longer open on broker (TP fill or external close)",
                                exit_price=position.take_profit,
                            ))

        pending_opened = self._check_ema5_touch(tick)
        if pending_opened is not None:
            events.append(pending_opened)

        self._update_state()
        return events

    def _enter(
        self,
        direction: Direction,
        reason: str,
        cross_candle_time_override: pd.Timestamp | None = None,
    ) -> OpenedTrade | None:
        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)
        result = self.executor.open_market_order(direction, lots, self.config.take_profit_usd)

        cross_candle_time = (
            cross_candle_time_override if cross_candle_time_override is not None else self.current_candle_time
        )
        self.position = DualPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self._compute_stop_loss(direction, result.price),
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, validated=True,
        )

        log_decision(
            self.config.symbol, "trade_entered", reason,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            stop_loss=self.position.stop_loss, balance=balance, pre_validated=True,
        )

        return OpenedTrade(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self.position.stop_loss,
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, is_fallback_entry=True,
        )

    def _close_position(self, category: str, reason: str, exit_price: float) -> ClosedTrade:
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
