"""dual_cross_confirmed_swap_adx ("confirmed-entry-only + ADX swap gate") —
strategy_variant=dual_cross_confirmed_swap_adx.

BACKTEST-ONLY as of 2026-08-20 — NEVER register in main.py's
STRATEGY_ENGINES until the user reviews backtest results and explicitly
approves a live deploy, same rule as every other variant born in this
project.

Built on user's explicit instruction 2026-08-20, after reviewing real
entry-type data: drop tick-based tolerance entry entirely, every entry is
close-confirmed only, and (as a direct structural consequence, also
explicitly requested) the $3 early-exit net goes away too — since a
close-confirmed entry has always opened already-validated (see
dual_cross_tight_exit's original design), removing tick-based entry means
NO position is EVER unvalidated any more, so that net's condition can
never be true. Rather than leave that dead code sitting around, this is a
genuinely new, simpler engine — not a config flag on top of
dual_cross_tight_exit_swap_confirm_adx.

Keeps, unchanged from dual_cross_tight_exit_swap_confirm_adx:
  - The 2-candle swap-confirm debounce (arm on first confirmed opposite
    cross, fire only if the very next candle still opposes the held
    direction).
  - The ADX(14) >= threshold gate on top of that (default 25.0) — swap
    only actually fires if ADX also clears the bar at the confirming
    candle; below that, blocked (category swap_blocked_low_adx), pending
    reversal thrown away, held position keeps running untouched.
  - $5 take-profit (broker-side), unchanged.

Structurally simpler than every dual_cross_tight_exit-family engine
before it:
  - Every position opens already validated — there is no "unvalidated"
    state at all in this engine, so there is also no "own-candle
    validation" step, no validation_failed category, no
    early_exit_unconfirmed category, and no one-tick-attempt-per-candle
    tracking (nothing to track — there are no tick-based attempts).
  - The $15 stop-loss is checked on every position from the moment it
    opens (previously only active once a tick-based entry had survived
    its own candle's close) — this is the one real risk trade-off of
    removing the $3 net: a bad entry's worst case before TP/swap now
    rides all the way to $15 instead of being capped at ~$3, since there
    is no tighter net any more. Explicitly acknowledged to the user
    before building.

Requires a df_with_emas that also has an "adx" column precomputed (see
bot.indicators.adx.compute_adx) — scripts/backtest.py wires this in
whenever config.swap_adx_filter is set. Does NOT require a
dual_cross_tight_exit config section at all (no tick tolerance, no early
net) — only stop_loss_usd, take_profit_usd, and swap_adx_filter.

**$5 gap + EMA5-pullback rule added 2026-08-20** (the original "day one"
rule, merged in from dual_cross_tight_exit_gap_ema5's design), applying
ONLY to the flat entry path — explicitly NOT to the swap's re-entry,
confirmed with the user via a concrete before-building example:

  - When a confirmed close-based cross would normally trigger a fresh
    entry from flat, first check the gap between that candle's close
    price and EMA13: gap = |close - ema13|.
  - gap < gap_threshold_usd (reuses the existing top-level
    gap_threshold_usd config field, $5 in the real configs): enter
    immediately, exactly as before this addition.
  - gap >= gap_threshold_usd: do NOT enter yet. Remember the setup
    (direction only) as pending, and wait for a later tick where price
    pulls back and touches EMA5. Only then does the trade actually open
    (still opens fully validated, same as every entry in this engine).
  - If a genuine confirmed cross happens in the OPPOSITE direction while
    a setup is still pending (EMA5 never touched), the pending setup is
    thrown away outright — no trade from it, ever.

The swap's own re-entry (when the 2-candle+ADX-confirmed reversal fires)
is completely UNCHANGED by this — it still opens immediately, at
whatever price, the instant the swap executes. Reasoning discussed with
the user: the swap already carries its own "is this real" confirmation
(2 candles + ADX); adding a pullback-wait on top of that could leave the
bot flat with no position right after closing the old one, missing the
very reversal the swap exists to catch, if price never pulls back to
EMA5. The user confirmed keeping the swap immediate rather than gating it
too, after seeing that trade-off explained with numbers.
"""
from __future__ import annotations

import logging
import math
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

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_confirmed_swap_adx")


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


class DualCrossConfirmedSwapAdxEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap_adx requires stop_loss_usd to be set."
            )
        if config.swap_adx_filter is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap_adx requires a "
                "'swap_adx_filter:' config section."
            )
        if config.gap_threshold_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap_adx requires gap_threshold_usd to be set "
                "(the $5 gap + EMA5-pullback rule on flat entries)."
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
        # Armed by a confirmed opposite cross while a position is held;
        # only survives exactly one more candle — see module docstring.
        self.pending_reversal_direction: Direction | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_confirmed_swap_adx"]

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

        # Reversal — requires 2 consecutive candles when a position is
        # held, then an ADX gate on top of that. Fresh entries from flat
        # are untouched (no pending concept applies there at all).
        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if self.position is not None:
                held_state = CrossState.ABOVE if self.position.direction == Direction.BUY else CrossState.BELOW
                if new_state is not None and new_state != held_state:
                    direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL
                    if self.pending_reversal_direction == direction:
                        # 2-candle confirmation just passed -> ADX gate.
                        adx_value = last_closed["adx"]
                        adx_ok = not math.isnan(adx_value) and adx_value >= self.config.swap_adx_filter.adx_threshold
                        if adx_ok:
                            events.append(self._close_position(
                                category="swapped_confirmed_reversal",
                                reason=(
                                    f"{direction.value} cross confirmed TWO candles in a row (this candle: "
                                    f"ema13={ema13:.2f}, ema21={ema21:.2f}, adx={adx_value:.1f} >= "
                                    f"{self.config.swap_adx_filter.adx_threshold:.1f}) -> closing the opposite "
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
                                )
                                if opened is not None:
                                    events.append(opened)
                        else:
                            log_decision(
                                self.config.symbol, "swap_blocked_low_adx",
                                f"{direction.value} cross confirmed TWO candles in a row (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f}) but adx="
                                f"{'nan' if math.isnan(adx_value) else f'{adx_value:.1f}'} < "
                                f"{self.config.swap_adx_filter.adx_threshold:.1f} -> swap BLOCKED, "
                                f"{self.position.direction.value} position keeps running",
                            )
                            self.pending_reversal_direction = None
                    else:
                        self.pending_reversal_direction = direction
                        log_decision(
                            self.config.symbol, "swap_pending",
                            f"{direction.value} candle close (ema13={ema13:.2f}, "
                            f"ema21={ema21:.2f}) opposes held {self.position.direction.value} position but "
                            f"not swapped yet -> waiting for next candle to confirm before acting",
                        )
                else:
                    if self.pending_reversal_direction is not None:
                        log_decision(
                            self.config.symbol, "pending_cancelled",
                            f"Pending {self.pending_reversal_direction.value} reversal not reconfirmed "
                            f"this candle -> cancelled, {self.position.direction.value} position keeps running",
                        )
                    self.pending_reversal_direction = None
            else:
                # Flat -> the ONLY entry path this engine has: a genuine,
                # already-closed-candle EMA13/21 cross, now gated by the
                # $5 gap + EMA5-pullback rule (see module docstring). No
                # tick-based tolerance path exists at all in this engine.
                self.pending_reversal_direction = None
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
        # this just checks an EXISTING position for the $15 stop-loss or a
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
