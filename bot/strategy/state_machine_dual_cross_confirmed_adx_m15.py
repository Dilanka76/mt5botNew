"""dual_cross_confirmed_adx_m15 ("ADX + M15 gated entries, close-and-flatten
reversal, no swap") — strategy_variant=dual_cross_confirmed_adx_m15.

Built 2026-08-22/23 from an extensive real-data investigation of the
2026-08-21 critical-incident SELL (entry 4558.90, confirming-candle
ADX=20.40) and the swap-churn hypothetical traced from it (see
[[project_dual_cross_and_cross_confirmed]] for the full history). Two
deliberate design changes from the currently-live
`dual_cross_confirmed_swap_adx`, both explicit user decisions after
walking through real numbers together:

1. **Entries are gated by BOTH ADX(14) >= threshold AND a higher
   timeframe (M15) agreeing with the direction** — not just close-price
   gap. Checked once, on the confirming candle, before either the
   immediate-entry or gap/EMA5-pullback path runs. Either check failing
   blocks the whole signal outright (no entry, no pending setup) — the
   bot waits for the next independent confirmed cross and checks that
   one fresh. Real data checked before building: every real loss traced
   in this project's swap-chop investigation had ADX well under 25 at
   its confirming candle; several real WINS also had ADX under 25 (e.g.
   the 12:05 SELL, ADX 18.70, won +$29.64) — this filter is NOT assumed
   to be a clean win, it trades some real wins away in exchange for
   avoiding real losses. Net effect on a full real backtest is not yet
   known; this was deployed to be judged on live results, not backtest,
   per explicit user instruction (same pattern as several earlier
   variants in this project).

2. **The swap is REMOVED entirely, replaced by close-and-flatten.**
   Previously, a 2-candle-confirmed + ADX-gated reversal would close the
   current position AND immediately open a new one in the opposite
   direction. Traced through real data (the hypothetical 7-swap chain
   that would have followed the 4558.90 SELL if its entry had passed
   ADX): waiting for 2 candles before flipping meant giving up real
   price distance toward the take-profit target (a genuine, quantified
   cost — ~$4.41 of the $5 TP target was already "spent" by the market
   moving during the wait, in one real traced example), AND the 2-candle
   debounce still let the position get whipsawed 7 times in a row during
   sustained chop before finally catching the real move. The
   close-and-flatten alternative, traced through the SAME real window:
   one small contained exit (-$45.06) then flat through the rest of the
   chop (every subsequent signal blocked by the entry gate, same ADX+M15
   filter as any fresh entry) then one clean win at the next real signal
   (+$29.64) — net -$15.42, meaningfully better than either the frozen
   real incident (-$166.14) or the traced 7-swap whipsaw (-$140.16).

   Mechanism: the moment a SINGLE candle confirms the opposite direction
   from a held position — no waiting for a second candle, no ADX check
   on this decision at all — close the position immediately (whatever
   the P/L) and go flat. Getting back into the market, in EITHER
   direction, now requires passing the exact same entry gate (confirmed
   cross + ADX + M15) as any other fresh entry — there is no special
   "swap re-entry" path left in this engine at all.

Keeps unchanged from `dual_cross_confirmed_swap_adx`:
  - No tick-based entry at all (confirmed-close-only).
  - The $5 gap/EMA5-pullback rule on flat entries (now additionally
    gated by ADX+M15 before this rule is even consulted).
  - No $3 net, no "unvalidated" state — every position opens fully
    validated.
  - $15 stop-loss (the only remaining backstop besides the
    close-and-flatten reversal rule) and $5 take-profit, unchanged.

M15 confirmation mechanics: this engine does NOT fetch its own M15 data
via the connector — matching the existing architecture where engines
receive data, they don't fetch it. `main.py`'s loop calls
`engine.update_m15_data(m15_df_with_emas)` once per iteration (detected
via `hasattr`, so every other engine is completely unaffected) with a
freshly fetched+EMA-computed M15 dataframe; this engine stores the
latest CLOSED M15 candle's EMA13/21 relationship and reads it whenever
an entry decision needs the M15 check. Open question, not yet resolved,
noted for future revisiting: should this use M15's last closed candle
(current choice — safer, mildly delayed) or M15's current still-forming
state (faster, less settled)?

Requires a df_with_emas that also has an "adx" column precomputed (see
bot.indicators.adx.compute_adx) — reuses config.swap_adx_filter for the
ADX period/threshold (name kept for config-schema continuity even though
this variant no longer has a swap; it now gates entries instead).

DEPLOYED LIVE 2026-08-23 to demo1_m1/demo1_m3 WITHOUT a prior backtest —
explicit user decision, judged on real results. See
[[project_dual_cross_and_cross_confirmed]]'s "HOW TO REVERT" section for
the previous variant's full details if this ever needs to be rolled back.
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

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_confirmed_adx_m15")


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
    is None."""
    direction: Direction
    reason: str
    cross_candle_time: pd.Timestamp | None
    gap: float


class DualCrossConfirmedAdxM15Engine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        if config.stop_loss_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_adx_m15 requires stop_loss_usd to be set."
            )
        if config.swap_adx_filter is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_adx_m15 requires a "
                "'swap_adx_filter:' config section (adx_period/adx_threshold — now gates entries)."
            )
        if config.gap_threshold_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_adx_m15 requires gap_threshold_usd to be set "
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
        # M15's latest CLOSED candle's EMA13/21 relationship — refreshed
        # by main.py calling update_m15_data() once per loop iteration.
        # None until the first M15 update arrives (fails safe: no M15
        # data yet -> entries blocked, same as an ADX failure).
        self.m15_state: CrossState | None = None

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_confirmed_adx_m15"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float:
        return (
            entry_price - self.config.stop_loss_usd if direction == Direction.BUY
            else entry_price + self.config.stop_loss_usd
        )

    def update_m15_data(self, m15_df_with_emas: pd.DataFrame) -> None:
        """Called by main.py once per loop iteration (detected via
        hasattr — every other engine is unaffected) with a freshly
        fetched M15 dataframe (ema13/ema21 already computed). Stores
        just the latest CLOSED M15 candle's ABOVE/BELOW state."""
        if len(m15_df_with_emas) < 2:
            self.m15_state = None
            return
        last_closed_m15 = m15_df_with_emas.iloc[-2]
        self.m15_state = _classify(float(last_closed_m15["ema13"]), float(last_closed_m15["ema21"]))

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

        if self.prev_ema13 is not None and self.prev_ema21 is not None:
            prev_state = _classify(self.prev_ema13, self.prev_ema21)
            new_state = _classify(ema13, ema21)
            is_confirmed_cross = prev_state is not None and new_state is not None and prev_state != new_state

            if self.position is not None:
                # Close-and-flatten reversal — NO 2-candle wait, NO ADX
                # check on this decision at all. A single candle
                # confirming the opposite direction is enough to exit
                # immediately. Getting back in requires passing the
                # normal entry gate below, like any other fresh signal.
                held_state = CrossState.ABOVE if self.position.direction == Direction.BUY else CrossState.BELOW
                if new_state is not None and new_state != held_state:
                    events.append(self._close_position(
                        category="closed_confirmed_reversal",
                        reason=(
                            f"{('SELL' if held_state == CrossState.ABOVE else 'BUY')} cross confirmed "
                            f"(ema13={ema13:.2f}, ema21={ema21:.2f}) opposing held "
                            f"{self.position.direction.value} -> closing immediately (no wait, no ADX "
                            f"check on this decision), going flat"
                        ),
                        exit_price=exit_price,
                    ))
            else:
                # Flat -> the ONLY entry path this engine has: a genuine
                # close-confirmed cross, gated by ADX + M15 BOTH agreeing
                # before the gap/EMA5 rule is even consulted.
                direction = (Direction.BUY if new_state == CrossState.ABOVE else Direction.SELL) if is_confirmed_cross else None
                if is_confirmed_cross:
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
                        adx_value = last_closed["adx"]
                        adx_ok = not math.isnan(adx_value) and adx_value >= self.config.swap_adx_filter.adx_threshold
                        expected_m15_state = CrossState.ABOVE if direction == Direction.BUY else CrossState.BELOW
                        m15_ok = self.m15_state == expected_m15_state
                        if not adx_ok or not m15_ok:
                            log_decision(
                                self.config.symbol, "entry_blocked_adx_or_m15",
                                f"{direction.value} cross confirmed (ema13={ema13:.2f}, ema21={ema21:.2f}) but "
                                f"adx={'nan' if math.isnan(adx_value) else f'{adx_value:.1f}'} "
                                f"({'OK' if adx_ok else 'FAIL'} vs {self.config.swap_adx_filter.adx_threshold:.1f}), "
                                f"m15_state={self.m15_state.value if self.m15_state else 'unknown'} "
                                f"({'OK' if m15_ok else 'FAIL'} vs expected {expected_m15_state.value}) "
                                f"-> ENTRY BLOCKED, staying flat, waiting for the next independent signal",
                            )
                        else:
                            opened = self._maybe_enter_or_pend(
                                direction, exit_price, ema13,
                                base_reason=(
                                    f"close-confirmed: candle closed with a genuine "
                                    f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                    f"ema21={ema21:.2f}, adx={adx_value:.1f} >= "
                                    f"{self.config.swap_adx_filter.adx_threshold:.1f}, m15 agrees)"
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
