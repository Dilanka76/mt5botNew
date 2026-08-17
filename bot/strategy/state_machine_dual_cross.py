"""The dual-cross state machine — strategy_variant=dual_cross.

Full spec: docs/STRATEGY_DUAL_CROSS_SPEC.md. Design notes:
docs/STRATEGY_DUAL_CROSS_IMPLEMENTATION_PLAN.md. This is a clean,
independent rewrite (does NOT subclass EMAScalpEngine) — the mechanism is
different enough from bot/strategy/state_machine.py's gap_threshold design
(no gap-threshold concept, no EMA5/EMA9, no single-position assumption)
that patching would be more error-prone than starting fresh. It reuses only
the stateless/shared pieces from state_machine.py: Direction, OpenPosition
(extended below), is_within_session, calculate_lots, log_decision,
POSITION_CLOSE_GRACE_PERIOD_SECONDS.

Entry (spec Β§3): on every tick, for every open candle, a provisional
EMA13/EMA21 is computed by blending the current tick price with the
PREVIOUS closed candle's real EMA13/EMA21 (never this candle's own
not-yet-final close). If the provisional pair is within
cross_tolerance_usd of each other AND the relationship has just flipped
versus the previous closed candle, enter immediately at that tick's price.
Runs continuously, every tick, as long as no entry has fired yet for that
candle's cross (spec Β§5: this check never stops just because a position is
already open — it keeps watching for the OPPOSITE direction).

Validation (spec Β§4): exactly once, at the close of the specific candle
that triggered a given position's entry — does the real, close-based
EMA13/EMA21 still show the same relationship? If not, close that position
immediately regardless of P/L. Never repeats for a position that already
passed its own validation.

Stop-loss (spec Β§4a): a fixed, mandatory $stop_loss_usd per position,
checked every tick, independent of validation and of any other position.

Concurrency (spec Β§5/Β§5a): up to 2 simultaneous opposite-direction
positions (self.positions is keyed by Direction, so same-direction
double-entry is structurally impossible). When the SECOND (concurrent)
position's own cross candle validates, the FIRST position is force-closed
right then at the current price; if it fails validation instead, only the
second position closes and the first is untouched. A signal blocked by the
2-position cap or by a closed session does NOT consume that candle's
one-shot-entry slot — a later qualifying tick in the same still-forming
candle can still enter if a slot frees up or the session opens.

Close-confirmed fallback entry (added 2026-08-17, not in the original
spec — see [[project-dual-cross-and-cross-confirmed]] for the real-world
case that motivated this): the tick-based path above can genuinely miss a
real cross — e.g. price moves through the tolerance zone faster than the
live tick-poll interval can sample it. If a candle closes showing a
genuine, confirmed EMA13/21 flip and NO tick-based entry fired during that
candle's own formation, this engine now enters at that close price as a
backup, subject to the exact same cap/same-direction/session guards as the
tick-based path. A fallback entry is validated=True immediately (it's
already confirmed by construction — there's no provisional guess left to
check later) — if it's concurrent, the opposite position is force-closed
right then, at the same moment, exactly as Β§5 already does for a
tick-based entry whose later validation succeeds. This does not change
anything about entries the tick-based path already catches; it only adds
a second chance for the ones it doesn't.
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
from bot.strategy.state_machine import (
    POSITION_CLOSE_GRACE_PERIOD_SECONDS,
    OpenPosition,
    TradeState,
)

logger = logging.getLogger("bot.strategy.state_machine_dual_cross")


def _opposite(direction: Direction) -> Direction:
    return Direction.SELL if direction == Direction.BUY else Direction.BUY


def _classify(ema13: float, ema21: float) -> CrossState | None:
    if ema13 > ema21:
        return CrossState.ABOVE
    if ema13 < ema21:
        return CrossState.BELOW
    return None  # exactly equal — indeterminate, treated as no signal


@dataclass
class DualPosition(OpenPosition):
    """OpenPosition (reused as-is from state_machine.py) plus the three
    fields this engine needs that the old single-position design never
    did. `invalid` (inherited) is not used here — that concept belongs to
    the old EMA5/EMA9 race, which doesn't exist in this design."""
    cross_candle_time: pd.Timestamp | None = None
    is_concurrent_entry: bool = False  # True iff another position was already open the instant this one entered
    validated: bool = False  # one-shot guard: has the Β§4 close-candle validation already run for this position?


@dataclass
class OpenedTrade:
    direction: Direction
    ticket: int | None
    entry_price: float
    take_profit: float
    stop_loss: float
    cross_candle_time: pd.Timestamp
    is_concurrent_entry: bool
    # True for a Β§4b close-confirmed fallback entry (see module docstring)
    # — False for the normal, tick-based path. Lets callers (reports,
    # backtest recording) distinguish the two instead of both looking
    # like an ordinary tick_cross entry.
    is_fallback_entry: bool = False


@dataclass
class ClosedTrade:
    direction: Direction
    ticket: int | None
    entry_price: float
    exit_price: float
    category: str
    reason: str


class DualCrossEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross requires stop_loss_usd to be set — the $15 "
                "stop-loss (spec Β§4a) is mandatory for this variant, not optional."
            )
        if config.dual_cross is None:
            raise ValueError(
                "strategy_variant=dual_cross requires a 'dual_cross:' config section — "
                "see docs/STRATEGY_DUAL_CROSS_SPEC.md."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.positions: dict[Direction, DualPosition] = {}
        # The previous CLOSED candle's real EMA13/21 — the only legitimate
        # base for every tick's provisional calculation. None until the
        # first candle has been processed.
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None
        # The currently-forming candle's own timestamp (read directly off
        # the live/forming row on_new_candle receives — see cross_detector.py's
        # module docstring on df.iloc[-1] being that row) — stamped onto any
        # position opened by on_tick, and what on_new_candle matches its
        # own close against to run that position's one-time validation.
        self.current_candle_time: pd.Timestamp | None = None
        self._entry_fired_this_candle = False

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def _reject_manual_positions(self) -> None:
        """Same rule as the gap_threshold engine's method of the same name
        (bot/strategy/state_machine.py) — duplicated here rather than
        shared, since this engine deliberately doesn't subclass that one."""
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
        """Adopts up to max_concurrent_positions pre-existing bot-owned
        (magic-matched) broker positions. A reconciled position has no
        tick/candle history from this run to validate against, so it
        starts validated=True (self-heals: skips its one-time Β§4 check
        forever, governed only by SL/TP/the concurrent-race rules from
        here on) and is_concurrent_entry=False (safe default). Any 3rd+
        matching-magic position found — which should never happen — is
        force-closed defensively rather than silently dropped."""
        self._reject_manual_positions()

        broker_positions = self.executor.get_open_positions()
        cap = self.config.dual_cross.max_concurrent_positions
        for position in broker_positions[:cap]:
            direction = Direction.BUY if position.type == mt5.ORDER_TYPE_BUY else Direction.SELL
            if direction in self.positions:
                log_decision(
                    self.config.symbol,
                    "reconcile_duplicate_direction_force_closed",
                    f"Found a second {direction.value} position on startup (ticket={position.ticket}) — "
                    f"should never happen, force-closing the duplicate defensively",
                    ticket=position.ticket,
                )
                try:
                    self.executor.close_position(position.ticket)
                except Exception:
                    logger.exception("Failed to force-close duplicate-direction position ticket=%s", position.ticket)
                continue
            self.positions[direction] = DualPosition(
                direction=direction,
                ticket=position.ticket,
                entry_price=position.price_open,
                take_profit=position.tp,
                stop_loss=self._compute_stop_loss(direction, position.price_open),
                cross_candle_time=None,
                is_concurrent_entry=False,
                validated=True,
            )
            log_decision(
                self.config.symbol,
                "position_reconciled",
                f"Adopted existing {direction.value} position on startup (validated=True, self-healed — "
                f"no candle history to validate against this run)",
                ticket=position.ticket,
                entry=position.price_open,
                tp=position.tp,
            )

        for position in broker_positions[cap:]:
            log_decision(
                self.config.symbol,
                "reconcile_extra_position_force_closed",
                f"Found a {cap + 1}th+ bot-owned position on startup (ticket={position.ticket}) — "
                f"should never happen, force-closing defensively",
                ticket=position.ticket,
            )
            try:
                self.executor.close_position(position.ticket)
            except Exception:
                logger.exception("Failed to force-close extra position ticket=%s", position.ticket)

        self._update_state()

    def _update_state(self) -> None:
        self.state = TradeState.IN_POSITION if self.positions else TradeState.IDLE

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> list[OpenedTrade | ClosedTrade]:
        """Call once per newly closed candle. df's iloc[-2] is the real,
        final candle just closed; iloc[-1] is the live/forming one (see
        cross_detector.py's module docstring for why -2, not -1)."""
        events: list[OpenedTrade | ClosedTrade] = []
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        exit_price = float(last_closed["close"])

        # Β§4: one-time validation, only for positions whose OWN triggering
        # cross candle is closing right now.
        for direction in list(self.positions.keys()):
            position = self.positions.get(direction)
            if position is None:
                continue  # already closed earlier in this same loop
            if position.validated or position.cross_candle_time != last_closed_time:
                continue
            position.validated = True

            matches = (
                (direction == Direction.BUY and ema13 > ema21)
                or (direction == Direction.SELL and ema13 < ema21)
            )
            if matches:
                log_decision(
                    self.config.symbol,
                    "position_validated",
                    f"{direction.value} position's own cross candle closed still confirming "
                    f"the {direction.value} relationship (ema13={ema13:.2f}, ema21={ema21:.2f})",
                    ticket=position.ticket,
                )
                if position.is_concurrent_entry:
                    opposite = _opposite(direction)
                    if opposite in self.positions:
                        events.append(self._close_position(
                            opposite,
                            category="closed_by_concurrent_validation",
                            reason=(
                                f"{direction.value}'s own cross candle validated -> closing the "
                                f"original {opposite.value} now, at its current price, regardless of P/L"
                            ),
                            exit_price=exit_price,
                        ))
            else:
                events.append(self._close_position(
                    direction,
                    category="validation_failed",
                    reason=(
                        f"{direction.value} position's own cross candle closed WITHOUT confirming "
                        f"(reverted to ema13={ema13:.2f}, ema21={ema21:.2f}) -> closing regardless of P/L"
                    ),
                    exit_price=exit_price,
                ))

        # Β§4b (close-confirmed fallback, see module docstring): this
        # candle's close shows a genuine cross that the tick-based path
        # never caught during the candle's own formation — enter now, at
        # the close, as a backup. Same guards as the tick-based path;
        # skipped entirely if anything already entered this candle
        # (either direction — one entry per candle is the existing rule,
        # unchanged here).
        if not self._entry_fired_this_candle and self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            if prev_state is not None and new_state is not None and prev_state != new_state:
                direction = Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL
                cap = self.config.dual_cross.max_concurrent_positions
                if len(self.positions) >= cap:
                    log_decision(
                        self.config.symbol, "entry_blocked_cap",
                        f"{direction.value} close-confirmed fallback blocked: already at "
                        f"{len(self.positions)}/{cap} positions",
                    )
                elif direction in self.positions:
                    # Structurally shouldn't happen — same reasoning as the
                    # tick-based path's identical guard.
                    log_decision(
                        self.config.symbol, "entry_blocked_same_direction",
                        f"{direction.value} close-confirmed fallback ignored: already holding a "
                        f"{direction.value} position (unexpected)",
                    )
                elif not is_within_session(self._active_sessions()):
                    log_decision(
                        self.config.symbol, "cross_ignored_outside_session",
                        f"{direction.value} close-confirmed fallback at candle close, no session open",
                    )
                else:
                    opened = self._enter(
                        direction,
                        reason=(
                            f"close-confirmed fallback: candle closed with a genuine "
                            f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                            f"ema21={ema21:.2f}) and no tick-based entry caught it this candle"
                        ),
                        cross_candle_time_override=last_closed_time,
                        pre_validated=True,
                        is_fallback_entry=True,
                    )
                    if opened is not None:
                        events.append(opened)
                        if opened.is_concurrent_entry:
                            opposite = _opposite(direction)
                            if opposite in self.positions:
                                events.append(self._close_position(
                                    opposite,
                                    category="closed_by_concurrent_validation",
                                    reason=(
                                        f"{direction.value}'s close-confirmed fallback entry is validated "
                                        f"immediately -> closing the original {opposite.value} now, at its "
                                        f"current price"
                                    ),
                                    exit_price=exit_price,
                                ))

        self.prev_ema13 = ema13
        self.prev_ema21 = ema21
        self._entry_fired_this_candle = False
        self.current_candle_time = df_with_emas.index[-1]

        self._update_state()
        return events

    def on_tick(self, tick) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        self._reject_manual_positions()

        live_tickets: set | None = None
        if self.config.execution.mode != "shadow" and self.positions:
            live_tickets = {p.ticket for p in self.executor.get_open_positions()}

        # Β§4a stop-loss + take-profit, every open position, independently.
        for direction in list(self.positions.keys()):
            position = self.positions.get(direction)
            if position is None:
                continue
            if time.monotonic() - position.opened_monotonic < POSITION_CLOSE_GRACE_PERIOD_SECONDS:
                continue

            stop_hit = (
                (direction == Direction.BUY and tick.bid <= position.stop_loss)
                or (direction == Direction.SELL and tick.bid >= position.stop_loss)
            )
            if stop_hit:
                events.append(self._close_position(
                    direction, category="stop_loss",
                    reason=f"${self.config.stop_loss_usd:.2f} stop-loss hit at {position.stop_loss:.2f}",
                    exit_price=position.stop_loss,
                ))
                continue

            if self.config.execution.mode == "shadow":
                tp_hit = (
                    (direction == Direction.BUY and tick.bid >= position.take_profit)
                    or (direction == Direction.SELL and tick.bid <= position.take_profit)
                )
                if tp_hit:
                    events.append(self._record_broker_closed(
                        direction, category="take_profit",
                        reason=f"[SHADOW] would have hit TP at {position.take_profit:.2f}",
                        exit_price=position.take_profit,
                    ))
            else:
                if position.ticket not in live_tickets:
                    events.append(self._record_broker_closed(
                        direction, category="take_profit",
                        reason="position no longer open on broker (TP fill or external close)",
                        exit_price=position.take_profit,
                    ))

        # Β§3 entry + Β§5 concurrent-watch + Β§5a cap.
        if (
            self.prev_ema13 is not None
            and self.prev_ema21 is not None
            and not self._entry_fired_this_candle
        ):
            k_mid = 2 / (self.config.ema_periods.mid + 1)
            k_slow = 2 / (self.config.ema_periods.slow + 1)
            prov13 = tick.bid * k_mid + self.prev_ema13 * (1 - k_mid)
            prov21 = tick.bid * k_slow + self.prev_ema21 * (1 - k_slow)

            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            prov_state = _classify(prov13, prov21)
            is_flip = prev_state is not None and prov_state is not None and prev_state != prov_state
            within_tolerance = abs(prov13 - prov21) <= self.config.dual_cross.cross_tolerance_usd

            if is_flip and within_tolerance:
                direction = Direction.BUY if prov_state == CrossState.ABOVE else Direction.SELL
                cap = self.config.dual_cross.max_concurrent_positions
                if len(self.positions) >= cap:
                    log_decision(
                        self.config.symbol, "entry_blocked_cap",
                        f"{direction.value} tick-cross signal blocked: already at "
                        f"{len(self.positions)}/{cap} positions — not consumed, still eligible later this candle",
                    )
                elif direction in self.positions:
                    # Structurally shouldn't happen (both directions share the
                    # same prev_ema13/21 baseline, so a same-direction signal
                    # without an intervening opposite flip is impossible) —
                    # defensive skip rather than a silent double-open.
                    log_decision(
                        self.config.symbol, "entry_blocked_same_direction",
                        f"{direction.value} tick-cross signal ignored: already holding a "
                        f"{direction.value} position (unexpected)",
                    )
                elif not is_within_session(self._active_sessions()):
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
                            f"${self.config.dual_cross.cross_tolerance_usd:.2f} "
                            f"(actual ${abs(prov13 - prov21):.2f}), flipped "
                            f"{prev_state.value}->{prov_state.value}"
                        ),
                    )
                    if opened is not None:
                        events.append(opened)
                        self._entry_fired_this_candle = True

        self._update_state()
        return events

    def _enter(
        self,
        direction: Direction,
        reason: str,
        cross_candle_time_override: pd.Timestamp | None = None,
        pre_validated: bool = False,
        is_fallback_entry: bool = False,
    ) -> OpenedTrade | None:
        """cross_candle_time_override/pre_validated/is_fallback_entry exist
        only for the Β§4b close-confirmed fallback (see on_new_candle) — a
        normal tick-based entry leaves all three at their defaults (uses
        self.current_candle_time, starts unvalidated, tagged as a normal
        tick entry, per Β§4)."""
        is_concurrent = len(self.positions) == 1  # captured before insertion below
        if (
            is_concurrent
            and self.config.execution.mode != "shadow"
            and self.config.dual_cross.require_hedging_account
            and not self.connector.is_hedging_account()
        ):
            # Should be structurally unreachable — main.py's own startup
            # check (see main.py) already hard-aborts a non-shadow,
            # non-hedging account before the loop ever runs. This is
            # defense-in-depth: skip just this one entry, log CRITICAL,
            # keep the bot (and the already-open first position) running.
            logger.critical(
                "dual_cross: refusing concurrent %s entry — account not confirmed hedging-mode "
                "(this should have been caught at startup)",
                direction.value,
            )
            log_decision(
                self.config.symbol, "entry_blocked_not_hedging",
                f"Refused concurrent {direction.value} entry: account is not confirmed hedging-mode",
            )
            return None

        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)
        result = self.executor.open_market_order(direction, lots, self.config.take_profit_usd)

        cross_candle_time = (
            cross_candle_time_override if cross_candle_time_override is not None else self.current_candle_time
        )
        position = DualPosition(
            direction=direction,
            ticket=result.ticket,
            entry_price=result.price,
            take_profit=result.take_profit,
            stop_loss=self._compute_stop_loss(direction, result.price),
            cross_candle_time=cross_candle_time,
            is_concurrent_entry=is_concurrent,
            validated=pre_validated,
        )
        self.positions[direction] = position

        log_decision(
            self.config.symbol, "trade_entered", reason,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            stop_loss=position.stop_loss, balance=balance, is_concurrent_entry=is_concurrent,
            pre_validated=pre_validated, is_fallback_entry=is_fallback_entry,
        )

        return OpenedTrade(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=position.stop_loss,
            cross_candle_time=cross_candle_time, is_concurrent_entry=is_concurrent,
            is_fallback_entry=is_fallback_entry,
        )

    def _close_position(self, direction: Direction, category: str, reason: str, exit_price: float) -> ClosedTrade:
        """Bot-initiated close (stop_loss, validation_failed,
        closed_by_concurrent_validation) — actively places a real close
        order via the executor."""
        position = self.positions.pop(direction)
        self.executor.close_position(position.ticket)
        log_decision(
            self.config.symbol, "trade_exited", reason,
            direction=direction.value, entry=position.entry_price, ticket=position.ticket, category=category,
        )
        return ClosedTrade(
            direction=direction, ticket=position.ticket, entry_price=position.entry_price,
            exit_price=exit_price, category=category, reason=reason,
        )

    def _record_broker_closed(self, direction: Direction, category: str, reason: str, exit_price: float) -> ClosedTrade:
        """The position is already gone at the broker (a real TP fill) or
        is being simulated as such (shadow mode) — just stop tracking it,
        no executor.close_position() call (that would be redundant or
        simply wrong, since there's nothing left to close)."""
        position = self.positions.pop(direction)
        log_decision(
            self.config.symbol, "trade_closed_tp", reason,
            direction=direction.value, ticket=position.ticket,
        )
        return ClosedTrade(
            direction=direction, ticket=position.ticket, entry_price=position.entry_price,
            exit_price=exit_price, category=category, reason=reason,
        )
