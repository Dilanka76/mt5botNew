"""BACKTEST-ONLY variant: dual_cross_tight_exit_gap_ema5.

Built 2026-08-19 at the user's explicit request to compare — never to
deploy live. Identical to dual_cross_tight_exit (see that engine's module
docstring for the full $3-net / reversal-swap / one-attempt-per-candle
design, all unchanged here) except for ONE addition, merged in from the
ORIGINAL gap_threshold engine (bot/strategy/state_machine.py): before
either entry path (tick-based or close-confirmed fallback) actually opens
a position, first check the gap between the current price and EMA13 at
that moment.

  - gap < gap_threshold_usd: enter immediately, exactly as
    dual_cross_tight_exit does today.
  - gap >= gap_threshold_usd: DON'T enter yet. Remember the setup
    (direction + reason) as pending, and wait for a live tick to touch
    EMA5. Only then does the trade actually open.

Three rules confirmed with the user before building, verbatim:
  1. If a genuine CONFIRMED cross happens in the OPPOSITE direction while
     a setup is pending (EMA5 not yet touched), the pending setup is
     cancelled outright — no trade from it, ever.
  2. An entry that opens via the EMA5-touch path is NOT subject to the
     $early_exit_usd net at all — it opens already pre_validated=True
     (same treatment as a close-confirmed fallback entry), on the
     reasoning that waiting for the pullback to EMA5 already provides
     enough confirmation that the extra $3 net isn't needed.
  3. The gap check applies to BOTH entry paths (tick-based AND
     close-confirmed fallback), not just one.

Reuses gap_threshold_usd (an existing top-level AppConfig field, already
used by the original gap_threshold engine) rather than adding a new
config field — this variant requires it to be set.

NOT wired into main.py's STRATEGY_ENGINES — this variant must never be
launched live. Only registered in bot/backtest/runner.py's
STRATEGY_ENGINES for scripts/backtest.py to use, always via
--settings-path pointing at a backtest-only config copy, never the real
live config/settings.<account>.yaml.
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

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_tight_exit_gap_ema5")


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


@dataclass
class PendingSetup:
    direction: Direction
    reason: str
    cross_candle_time: pd.Timestamp | None
    gap: float


class DualCrossTightExitGapEma5Engine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit_gap_ema5 requires stop_loss_usd to be set."
            )
        if config.dual_cross_tight_exit is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit_gap_ema5 requires a "
                "'dual_cross_tight_exit:' config section (reused unchanged)."
            )
        if config.gap_threshold_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit_gap_ema5 requires gap_threshold_usd to be set."
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
        self._tick_entry_used_this_candle = False

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_tight_exit_gap_ema5"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def _early_exit_hit(self, position: DualPosition, bid: float) -> bool:
        adverse = (
            position.entry_price - bid if position.direction == Direction.BUY
            else bid - position.entry_price
        )
        return adverse >= self.config.dual_cross_tight_exit.early_exit_usd

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
            self.position = DualPosition(
                direction=direction, ticket=broker_position.ticket, entry_price=broker_position.price_open,
                take_profit=broker_position.tp, stop_loss=self._compute_stop_loss(direction, broker_position.price_open),
                cross_candle_time=None, is_concurrent_entry=False, validated=True,
            )
            log_decision(
                self.config.symbol, "position_reconciled",
                f"Adopted existing {direction.value} position on startup (validated=True, self-healed)",
                ticket=broker_position.ticket, entry=broker_position.price_open, tp=broker_position.tp,
            )
        self._update_state()

    def _update_state(self) -> None:
        self.state = TradeState.IN_POSITION if self.position is not None else TradeState.IDLE

    def _maybe_enter_or_pend(
        self, direction: Direction, price_now: float, ema13_now: float, base_reason: str,
        cross_candle_time_override: pd.Timestamp | None, pre_validated_if_immediate: bool,
    ) -> OpenedTrade | None:
        """Shared gap-check for both entry paths. gap = distance between
        price_now and ema13_now, same measurement the original
        gap_threshold engine used (bot/strategy/state_machine.py's
        _decide_entry)."""
        gap = (price_now - ema13_now) if direction == Direction.BUY else (ema13_now - price_now)
        gap = abs(gap)
        if gap < self.config.gap_threshold_usd:
            return self._enter(
                direction,
                reason=f"{base_reason}, gap={gap:.2f} < ${self.config.gap_threshold_usd:.2f} threshold -> immediate entry",
                cross_candle_time_override=cross_candle_time_override,
                pre_validated=pre_validated_if_immediate,
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

        # (1) Own-candle validation for a not-yet-validated position.
        if (
            self.position is not None
            and not self.position.validated
            and self.position.cross_candle_time == last_closed_time
        ):
            position = self.position
            position.validated = True
            matches = (
                (position.direction == Direction.BUY and ema13 > ema21)
                or (position.direction == Direction.SELL and ema13 < ema21)
            )
            if matches:
                log_decision(
                    self.config.symbol, "position_validated",
                    f"{position.direction.value} position's own cross candle closed still confirming "
                    f"(ema13={ema13:.2f}, ema21={ema21:.2f}) — never hit the "
                    f"${self.config.dual_cross_tight_exit.early_exit_usd:.2f} early-exit net",
                    ticket=position.ticket,
                )
            else:
                events.append(self._close_position(
                    category="validation_failed",
                    reason=(
                        f"{position.direction.value} position's own cross candle closed WITHOUT "
                        f"confirming (ema13={ema13:.2f}, ema21={ema21:.2f}) -> closing at candle close"
                    ),
                    exit_price=exit_price,
                ))

        # (2) Reversal swap / close-confirmed fallback entry, plus pending
        # setup cancellation on a genuine opposite confirmed cross.
        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if is_confirmed_cross:
                direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL

                # Rule 1: cancel a pending setup if a confirmed cross
                # happens in the OPPOSITE direction before EMA5 was touched.
                if self.pending is not None and self.pending.direction != direction:
                    log_decision(
                        self.config.symbol, "pending_cancelled",
                        f"{direction.value} cross confirmed at candle close -> cancelling pending "
                        f"{self.pending.direction.value} setup (EMA5 never touched, gap was {self.pending.gap:.2f})",
                    )
                    self.pending = None

                if self.position is not None and self.position.direction == direction:
                    pass  # same-direction reconfirm — nothing to do
                else:
                    if self.position is not None:
                        events.append(self._close_position(
                            category="swapped_confirmed_reversal",
                            reason=(
                                f"{direction.value} cross confirmed at candle close (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f}) -> closing the opposite "
                                f"{self.position.direction.value} now, regardless of P/L"
                            ),
                            exit_price=exit_price,
                        ))

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
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                            pre_validated_if_immediate=True,
                        )
                        if opened is not None:
                            events.append(opened)

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self.current_ema5 = ema5
        self._tick_entry_used_this_candle = False
        self.current_candle_time = df_with_emas.index[-1]
        self._update_state()
        return events

    def _check_ema5_touch(self, tick) -> OpenedTrade | None:
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
            pre_validated=True,  # Rule 2: no $3 net for EMA5-touch entries.
        )
        self.pending = None
        return opened

    def on_tick(self, tick) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        self._reject_manual_positions()

        live_tickets: set | None = None
        if self.config.execution.mode != "shadow" and self.position is not None:
            live_tickets = {p.ticket for p in self.executor.get_open_positions()}

        if self.position is not None:
            position = self.position
            if time.monotonic() - position.opened_monotonic >= POSITION_CLOSE_GRACE_PERIOD_SECONDS:
                if position.validated:
                    stop_hit = (
                        (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                        or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
                    )
                else:
                    stop_hit = False
                if stop_hit:
                    events.append(self._close_position(
                        category="stop_loss",
                        reason=f"${self.config.stop_loss_usd:.2f} stop-loss hit at {position.stop_loss:.2f}",
                        exit_price=position.stop_loss,
                    ))
                elif not position.validated and self._early_exit_hit(position, tick.bid):
                    events.append(self._close_position(
                        category="early_exit_unconfirmed",
                        reason=(
                            f"${self.config.dual_cross_tight_exit.early_exit_usd:.2f} early-exit hit at "
                            f"{tick.bid:.2f} before this position's own cross candle confirmed"
                        ),
                        exit_price=tick.bid,
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

        # EMA5-touch check for a pending setup — takes priority over a
        # fresh tick-based signal this same tick (both require
        # self.position is None; checking pending first is enough since
        # _maybe_enter_or_pend below only runs when nothing just opened).
        pending_opened = self._check_ema5_touch(tick)
        if pending_opened is not None:
            events.append(pending_opened)
            self._tick_entry_used_this_candle = True

        # Tick-based entry — only when no position AND no pending setup is
        # active, and no tick-based entry already attempted this candle.
        allow_retry = (
            not self._tick_entry_used_this_candle
            or self.config.dual_cross_tight_exit.allow_multiple_tick_attempts_per_candle
        )
        if (
            self.position is None
            and self.pending is None
            and allow_retry
            and self.prev_ema13 is not None
            and self.prev_ema21 is not None
        ):
            k_mid = 2 / (self.config.ema_periods.mid + 1)
            k_slow = 2 / (self.config.ema_periods.slow + 1)
            prov13 = tick.bid * k_mid + self.prev_ema13 * (1 - k_mid)
            prov21 = tick.bid * k_slow + self.prev_ema21 * (1 - k_slow)

            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            prov_state = _classify(prov13, prov21)
            is_flip = prev_state is not None and prov_state is not None and prev_state != prov_state
            within_tolerance = abs(prov13 - prov21) <= self.config.dual_cross_tight_exit.cross_tolerance_usd

            if is_flip and within_tolerance:
                direction = Direction.BUY if prov_state == CrossState.ABOVE else Direction.SELL
                if not is_within_session(self._active_sessions()):
                    log_decision(
                        self.config.symbol, "cross_ignored_outside_session",
                        f"{direction.value} tick-cross at bid={tick.bid:.2f}, no session open — "
                        f"not consumed, still eligible later this candle if session opens",
                    )
                else:
                    opened = self._maybe_enter_or_pend(
                        direction, tick.bid, prov13,
                        base_reason=(
                            f"tick-cross: provisional EMA13/21 within "
                            f"${self.config.dual_cross_tight_exit.cross_tolerance_usd:.2f} "
                            f"(actual ${abs(prov13 - prov21):.2f}), flipped {prev_state.value}->{prov_state.value}"
                        ),
                        cross_candle_time_override=None,
                        pre_validated_if_immediate=False,
                    )
                    if opened is not None:
                        events.append(opened)
                        self._tick_entry_used_this_candle = True
                    elif self.pending is not None:
                        # A pending setup was just created via the tick path
                        # this candle — counts as "used" for the fallback gate.
                        self._tick_entry_used_this_candle = True

        self._update_state()
        return events

    def _enter(
        self,
        direction: Direction,
        reason: str,
        cross_candle_time_override: pd.Timestamp | None = None,
        pre_validated: bool = False,
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
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, validated=pre_validated,
        )

        log_decision(
            self.config.symbol, "trade_entered", reason,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            stop_loss=self.position.stop_loss, balance=balance, pre_validated=pre_validated,
        )

        return OpenedTrade(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self.position.stop_loss,
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, is_fallback_entry=pre_validated,
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
