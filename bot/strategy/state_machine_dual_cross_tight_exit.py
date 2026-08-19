"""The dual-cross-tight-exit state machine — strategy_variant=
dual_cross_tight_exit.

Built on user request (2026-08-19), sitting alongside dual_cross and
dual_cross_confirmed_entry without changing either. Designed directly from
a real-trade-history finding: on demo1_m1/demo1_m3 over 2026-08-17/18,
96.9% of dual_cross's real losing trades traced back to two mechanisms —
validation_failed (a tick-based entry whose own candle failed to confirm
it, force-closed at whatever the candle's close price happened to be, no
matter how far price had moved) and closed_by_concurrent_validation (an
opposite confirmed cross displacing one of two simultaneously-held
positions). This variant keeps dual_cross's actual edge — entering early,
on a tick-based signal, before the candle even finishes forming — but
bolts on two protections aimed squarely at those two failure modes:

1. A tight $early_exit_usd net, checked every tick, that ONLY watches for
   adverse movement on a not-yet-validated position. If price moves that
   far against an unconfirmed position, it's closed immediately at that
   small, capped loss — instead of dual_cross's behavior of waiting
   blindly for the candle to close and taking whatever that produces
   (real data showed losses up to -$28.50 from this).
2. A reversal swap: at ANY point, validated or not, if a later candle's
   real close shows a genuine confirmed cross opposite the currently held
   position, that position is closed immediately (whatever its current
   P/L) and the new confirmed-direction position opens right away. This
   replaces dual_cross's concurrent-position mechanism (which requires a
   hedging account and briefly holds two real tickets) with a plain
   single-position swap — same "get out when the market proves you
   wrong" protection, cheaper and operationally simpler.

Structural consequence, confirmed with the user before building: because
of (2), this engine can never hold two positions at once (single-position,
auto-replacing design, like dual_cross_confirmed_entry) — no position cap,
no hedging-account requirement.

Entry priority: AT MOST ONE tick-based attempt per candle (checked
continuously, whenever no position is open AND no tick-based entry has
already been attempted this candle) — tracked by
_tick_entry_used_this_candle, reset only when a new candle starts. If
that one attempt hits the early-exit net and closes, no further
tick-based entries are tried for the rest of that candle; the ONLY way a
new position can open for the remainder of that candle is the
close-confirmed fallback (Β§4b-style, at the candle's real close). This
was explicitly confirmed with the user 2026-08-19 after an earlier draft
of this engine allowed unlimited same-candle tick-based retries — every
tick-based check within one candle compares against the SAME fixed
pre-candle EMA13/21 baseline, so repeated retries could only ever flip
toward the same direction as the original attempt anyway; capping it at
one attempt avoids repeatedly gambling on that single direction and
instead waits for the real candle close to decide.

The $15 stop-loss and the $3 early-exit net are mutually exclusive on a
single position, never both checked: a position watches ONLY early_exit_usd
while unvalidated, and ONLY the real stop_loss_usd once validated — not
"whichever is smaller," structurally one or the other depending on state.

Reuses DualPosition/OpenedTrade/ClosedTrade from state_machine_dual_cross.py
(needs the validated/cross_candle_time fields DualPosition adds over the
plain OpenPosition) so bot/backtest/runner.py's existing event-consuming
recording path works for this engine unchanged too.
"""
from __future__ import annotations

import logging
import time

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

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_tight_exit")


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


class DualCrossTightExitEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit requires stop_loss_usd to be set."
            )
        if config.dual_cross_tight_exit is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit requires a "
                "'dual_cross_tight_exit:' config section."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: DualPosition | None = None
        # The previous CLOSED candle's real EMA13/21 — baseline for every
        # tick's provisional calculation and for detecting a genuine flip
        # at each candle's own close (both entry and the reversal swap).
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None
        self.current_candle_time: pd.Timestamp | None = None
        # True iff a tick-based entry has already succeeded at least once
        # during the candle that's currently forming — gates ONLY the
        # close-confirmed fallback (see module docstring for why an early
        # exit does NOT reset this: a fresh tick-based retry is always
        # allowed, the fallback specifically is not needed twice).
        self._tick_entry_used_this_candle = False

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_tight_exit"]

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
                f"Adopted existing {direction.value} position on startup (validated=True, self-healed — "
                f"no candle history to validate against this run)",
                ticket=broker_position.ticket, entry=broker_position.price_open, tp=broker_position.tp,
            )
        self._update_state()

    def _update_state(self) -> None:
        self.state = TradeState.IN_POSITION if self.position is not None else TradeState.IDLE

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> list[OpenedTrade | ClosedTrade]:
        """Runs, in order, every time a new candle closes: (1) own-candle
        validation for a not-yet-validated position (only relevant if it
        survived the whole candle without hitting the early-exit net), (2)
        the reversal swap / close-confirmed-fallback-entry check, using the
        candle's real close (not a tick-based guess)."""
        events: list[OpenedTrade | ClosedTrade] = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        exit_price = float(last_closed["close"])

        # (1) Own-candle validation — only for a position whose OWN entry
        # candle is closing right now, and only if it's still unvalidated
        # (i.e. it survived the whole candle without hitting early_exit_usd).
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
                        f"confirming (ema13={ema13:.2f}, ema21={ema21:.2f}), but never hit the "
                        f"${self.config.dual_cross_tight_exit.early_exit_usd:.2f} early-exit net either "
                        f"-> closing at candle close regardless"
                    ),
                    exit_price=exit_price,
                ))

        # (2) Reversal swap (any held position, validated or not) / entry
        # (either the tick-based path already got in this candle, in which
        # case this is a no-op for entry purposes, or it's the
        # close-confirmed fallback catching a genuine miss).
        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if is_confirmed_cross:
                direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL

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
                        opened = self._enter(
                            direction,
                            reason=(
                                f"close-confirmed: candle closed with a genuine "
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                            pre_validated=True,
                        )
                        if opened is not None:
                            events.append(opened)

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self._tick_entry_used_this_candle = False
        self.current_candle_time = df_with_emas.index[-1]
        self._update_state()
        return events

    def on_tick(self, tick) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        self._reject_manual_positions()

        live_tickets: set | None = None
        if self.config.execution.mode != "shadow" and self.position is not None:
            live_tickets = {p.ticket for p in self.executor.get_open_positions()}

        if self.position is not None:
            position = self.position
            if time.monotonic() - position.opened_monotonic >= POSITION_CLOSE_GRACE_PERIOD_SECONDS:
                # The $15 stop-loss and the $3 early-exit net are mutually
                # exclusive, never both checked on the same position: a
                # validated (confirmed) position is watched ONLY by the
                # real $15 stop-loss; an unvalidated one is watched ONLY
                # by early_exit_usd. Confirmed with the user 2026-08-19 —
                # not just "the $3 net always fires first in practice" but
                # structurally impossible for the $15 stop to ever apply
                # before confirmation.
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

        # Tick-based entry — only when no position is currently open
        # (single-position design) AND no tick-based entry has already
        # been attempted this candle. Confirmed with the user 2026-08-19:
        # ONE tick-based attempt per candle only — if it hits the
        # early-exit net and closes, the rest of that candle's entries can
        # ONLY come from the close-confirmed fallback below (waiting for
        # the real candle close), never another tick-based guess. This is
        # a deliberate change from an earlier draft of this engine, which
        # allowed unlimited same-candle tick-based retries.
        if (
            self.position is None
            and not self._tick_entry_used_this_candle
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
                    opened = self._enter(
                        direction,
                        reason=(
                            f"tick-cross: provisional EMA13/21 within "
                            f"${self.config.dual_cross_tight_exit.cross_tolerance_usd:.2f} "
                            f"(actual ${abs(prov13 - prov21):.2f}), flipped "
                            f"{prev_state.value}->{prov_state.value}"
                        ),
                    )
                    if opened is not None:
                        events.append(opened)
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
        """Bot-initiated close (stop_loss, early_exit_unconfirmed,
        validation_failed, swapped_confirmed_reversal) — actively places a
        real close order via the executor."""
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
