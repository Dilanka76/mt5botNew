"""dual_cross_tight_exit_swap_confirm ("the swap flip fix") —
strategy_variant=dual_cross_tight_exit_swap_confirm.

Built 2026-08-20, initially backtest-only for comparison, then deployed
live to demo1_m1/demo1_m3 the same day after the user reviewed backtest
results (see project memory for the numbers and the important caveat:
the backtest did not reproduce the current strategy's own real result for
the same period, so this was deployed to be judged on live results, not
backtest). Identical to dual_cross_tight_exit (see that engine's module
docstring for the full $3-net / one-attempt-per-candle /
SL-net-mutual-exclusivity design, all unchanged here) except for ONE
thing: the reversal swap now requires TWO consecutive candles to agree
before it actually executes, instead of firing on the very first
confirmed opposite cross.

Root cause this targets, found from real-trade analysis 2026-08-19/20:
swapped_confirmed_reversal was 73-75% of all real loss $ on both accounts
— the swap can't tell a genuine trend reversal apart from EMA13/21 chop
oscillating back and forth in a ranging market, and real trade clusters
showed exactly that (5+ swaps within an hour, one pair only 3 minutes
apart, both losing).

Mechanism: when a position is held and a candle closes confirming the
OPPOSITE direction, this does NOT swap immediately — it arms a pending
reversal (direction only, no trade yet). On the VERY NEXT candle close:
  - If that candle ALSO confirms the same opposite direction -> the swap
    executes now (same close+reopen behavior as dual_cross_tight_exit,
    just one candle later, and using price from this second candle
    instead of the first).
  - Anything else (no cross that candle, a same-direction reconfirm, or a
    DIFFERENT direction than what was pending) -> the pending reversal is
    thrown away, the current position keeps running untouched, as if the
    first candle's flip never happened.
A pending reversal only ever survives exactly one candle — it is not
carried forward indefinitely waiting for eventual confirmation.

Explicitly confirmed with the user before building, all three:
  1. Applies ONLY to the swap (an existing position being reversed).
     Fresh entries from flat (no position held) are completely unaffected
     — both the tick-based path and the close-confirmed fallback path
     still fire on the exact same candle as today, no waiting.
  2. The $3 early-exit net, one-attempt-per-candle rule, and $15-stop/net
     mutual exclusivity are all unchanged — this only touches the swap.
  3. Real trade-offs acknowledged, not hidden: a genuine fast reversal
     can be delayed a candle (worse entry price) or missed entirely if
     its confirming candle happens to wobble; suppressing a swap doesn't
     remove the position's risk, it just shifts exposure toward the real
     $15 stop-loss instead of a cheaper swap exit if the market really
     was turning. This is exactly why it was proven on backtest AND real
     data before being trusted, not backtest alone.

Wired into main.py's STRATEGY_ENGINES (deployable live) as of 2026-08-20,
and still registered in bot/backtest/runner.py's STRATEGY_ENGINES too for
ongoing backtest comparisons via scripts/backtest.py --settings-path.
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

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm")


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


class DualCrossTightExitSwapConfirmEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit_swap_confirm requires stop_loss_usd to be set."
            )
        if config.dual_cross_tight_exit is None:
            raise ValueError(
                "strategy_variant=dual_cross_tight_exit_swap_confirm requires a "
                "'dual_cross_tight_exit:' config section (reused unchanged)."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.position: DualPosition | None = None
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None
        self.current_candle_time: pd.Timestamp | None = None
        self._tick_entry_used_this_candle = False
        # Armed by a confirmed opposite cross while a position is held;
        # only survives exactly one more candle — see module docstring.
        self.pending_reversal_direction: Direction | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_tight_exit_swap_confirm"]

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

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        exit_price = float(last_closed["close"])

        # (1) Own-candle validation — unchanged from dual_cross_tight_exit.
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

        # (2) Reversal — now requires 2 consecutive candles when a
        # position is held. Fresh entries from flat are untouched (no
        # pending concept applies there at all).
        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if self.position is not None:
                # Swap logic driven by "is THIS candle's real state opposite
                # of what we hold" — NOT by whether this candle itself is a
                # fresh flip vs the one before it. A held position's second
                # confirming candle usually ISN'T a fresh flip (it can't
                # flip again from a state it's already in) — it just needs
                # to still be on the opposite side. Bug found and fixed via
                # local smoke test 2026-08-20: an earlier draft required
                # is_confirmed_cross on the second candle too, which is
                # structurally almost never true and silently cancelled
                # every pending reversal instead of confirming it.
                held_state = CrossState.ABOVE if self.position.direction == Direction.BUY else CrossState.BELOW
                if new_state is not None and new_state != held_state:
                    direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL
                    if self.pending_reversal_direction == direction:
                        # Reconfirmed on the very next candle -> swap now.
                        events.append(self._close_position(
                            category="swapped_confirmed_reversal",
                            reason=(
                                f"{direction.value} cross confirmed TWO candles in a row (this candle: "
                                f"ema13={ema13:.2f}, ema21={ema21:.2f}) -> closing the opposite "
                                f"{self.position.direction.value} now, regardless of P/L"
                            ),
                            exit_price=exit_price,
                        ))
                        self.pending_reversal_direction = None
                        if not is_within_session(self._active_sessions()):
                            log_decision(
                                self.config.symbol, "cross_ignored_outside_session",
                                f"{direction.value} confirmed cross (2-candle) at {last_closed_time}, no session open",
                            )
                        else:
                            opened = self._enter(
                                direction,
                                reason=(
                                    f"close-confirmed (2-candle reversal): candle still shows {direction.value} "
                                    f"(ema13={ema13:.2f}, ema21={ema21:.2f}), confirmed again after the prior "
                                    f"candle's first signal"
                                ),
                                cross_candle_time_override=last_closed_time,
                                pre_validated=True,
                            )
                            if opened is not None:
                                events.append(opened)
                    else:
                        # First time seeing this direction (or a different
                        # direction than whatever was pending) -> arm it,
                        # take no action yet.
                        self.pending_reversal_direction = direction
                        log_decision(
                            self.config.symbol, "swap_pending",
                            f"{direction.value} candle close (ema13={ema13:.2f}, "
                            f"ema21={ema21:.2f}) opposes held {self.position.direction.value} position but "
                            f"not swapped yet -> waiting for next candle to confirm before acting",
                        )
                else:
                    # This candle matches (or reconfirms) the held direction,
                    # or is indeterminate -> any pending note is now stale
                    # (didn't get reconfirmed in time), clear it.
                    if self.pending_reversal_direction is not None:
                        log_decision(
                            self.config.symbol, "pending_cancelled",
                            f"Pending {self.pending_reversal_direction.value} reversal not reconfirmed "
                            f"this candle -> cancelled, {self.position.direction.value} position keeps running",
                        )
                    self.pending_reversal_direction = None
            else:
                # Flat -> normal close-confirmed fallback entry, exact
                # same candle, no 2-candle rule (nothing to protect). Still
                # driven by a genuine FRESH flip vs the previous candle,
                # unchanged from dual_cross_tight_exit.
                self.pending_reversal_direction = None
                direction = (Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL) if is_confirmed_cross else None
                if is_confirmed_cross:
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

        allow_retry = (
            not self._tick_entry_used_this_candle
            or self.config.dual_cross_tight_exit.allow_multiple_tick_attempts_per_candle
        )
        if (
            self.position is None
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
