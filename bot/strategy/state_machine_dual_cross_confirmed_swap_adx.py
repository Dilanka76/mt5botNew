"""dual_cross_confirmed_swap_adx ("confirmed-entry-only + ADX swap gate") —
strategy_variant=dual_cross_confirmed_swap_adx.

Deployed LIVE to demo1_m1/demo1_m3 2026-08-20, WITHOUT a prior backtest —
explicit user decision, made the same way several earlier variants in
this project were: judge it on real results, not backtest P/L (see
[[feedback-live-trading-discipline]]). No real-trade data exists for this
variant yet at deploy time.

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

**Pending-reversal stop-loss tightening, added 2026-08-24 (NOT YET
DEPLOYED — built, awaiting explicit user go-ahead).** User's concern: the
2-candle+ADX debounce is good at avoiding whipsaw flips, but during the
wait for the second candle, the HELD position keeps carrying its full
original stop-loss risk even though the first opposing candle is already
real evidence something may be going wrong. Solution, built after
tracing real 2026-08-24 data (both a case where this helps — two quick
stop-loss losses that would have been roughly halved — and a case where
it wouldn't have hurt — a trade with two false-alarm warnings that still
recovered and won, where price never got close to where a tightened stop
would sit):

  - The instant the FIRST opposing candle arms `pending_reversal_direction`
    (logged as `swap_pending`), the held position's stop-loss is
    immediately tightened to HALF the account's normal stop distance
    (e.g. $7.50 instead of $15 on demo1_m3, $2.50 instead of $5 on
    demo1_m1) — computed fresh from the position's own entry price via
    `_compute_stop_loss(direction, entry_price, distance_usd)`'s new
    optional `distance_usd` parameter (defaults to the full
    `config.stop_loss_usd` everywhere else it's called — entries, and
    reconcile_on_startup's self-heal path — only the swap_pending branch
    passes the halved value).
  - If the swap then fires (2nd candle + ADX both pass): moot — the old
    position closes at market regardless of its stop-loss value, and the
    NEW position opens with its own fresh, full-size stop.
  - If the swap is blocked by ADX (2nd candle confirms, ADX too weak):
    the tightened stop STAYS tightened for the rest of that position's
    life — explicit user decision — two candles opposing the trade in a
    row is a real warning sign even without ADX confirming, so the extra
    protection is kept rather than restored to full.
  - If no second opposing candle ever arrives and the market goes back in
    the position's favor: the tightened stop just sits there unused
    unless price happens to dip into it first — not explicitly
    traded-off against restoring to full in that specific case, since it
    never came up in the real examples checked; worth watching for.
  - `on_tick()`'s stop-loss-hit log line now reports the ACTUAL distance
    that fired (`abs(entry_price - stop_loss)`), not always the base
    `config.stop_loss_usd` — it would otherwise misleadingly say e.g.
    "$5.00 stop-loss hit" even when the tightened $2.50 stop is what
    actually triggered.

**ADX-momentum entry filter — added 2026-08-24/25, REMOVED 2026-08-27.**
Required ADX to be RISING versus the previous candle on every flat entry
(not a fixed threshold like 25 — that was explicitly rejected, an
earlier fixed-threshold design had blocked the vast majority of real
signals). Built after tracing two real losses where ADX was already
falling before entry and a violent single-candle spike hit the stop
before any EMA reversal could register.

Removed after real evidence outgrew the two losses that motivated it:
`scripts/simulate_blocked_adx_signals.py` automatically traced every one
of the 41 real signals this filter blocked across its two days live
(cross-checked against 4 hand-traced signals first, exact match on all
4) — 27 would-be wins (~+$810) vs. 14 would-be losses (~-$720), net
**~+$90 that the filter cost, not saved**. It was blocking more good
trades than bad ones. Explicit user decision to remove
("i think both 1m and 3m no need that... it makes miss changes").
`self.prev_adx` and its tracking were removed too, not just disabled —
no longer read anywhere. Every flat entry now takes any confirmed
EMA13/21 cross again, exactly as before 2026-08-24, gated only by the
$5 gap + EMA5-pullback rule below.

Does NOT affect the swap/reversal path — that still keeps its own
unchanged 2-candle + fixed-25-threshold ADX gate, plus the
pending-reversal stop-tightening above (both still live, both
unaffected by this removal).
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

    def _compute_stop_loss(self, direction: Direction, entry_price: float, distance_usd: float | None = None) -> float:
        distance = distance_usd if distance_usd is not None else self.config.stop_loss_usd
        return (
            entry_price - distance if direction == Direction.BUY
            else entry_price + distance
        )

    def _breakeven_stop_price(self, direction: Direction, entry_price: float) -> float:
        """Where the stop goes once breakeven arms -- exactly the entry
        price by default, or entry +/- config.breakeven_lock_usd if set
        (locks in a small guaranteed profit instead of exactly $0 on a
        reversal). Explicit user decision 2026-09-02, M1 accounts only
        (see settings.demo1_m1.yaml / settings.demo2_m1.yaml)."""
        lock = self.config.breakeven_lock_usd or 0.0
        return entry_price + lock if direction == Direction.BUY else entry_price - lock

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

    def _shadow_filter_info(self, direction: Direction, candle, df_with_emas: pd.DataFrame) -> dict:
        """SHADOW-ONLY, 2026-09-01: computes (but never acts on) what the
        experimental demo3 entry filters
        (state_machine_dual_cross_confirmed_swap_adx_entryfilter.py) would
        have decided for this REAL entry, and attaches it to the
        trade_entered log line. Does NOT change trading behavior at all --
        every real signal here still enters exactly as before. Purpose:
        after 1-2 weeks of real forward data, check whether trades marked
        "would have failed" here actually tended to be the losing ones,
        matching scripts/simulate_demo3_entry_filter.py's backtest
        (demo1_m1: color filter looked real, +$122.58 backtested; demo1_m3:
        volume filter looked real, +$151.02 backtested) -- BEFORE deciding
        whether to actually turn either filter on for real. See
        [[project_demo3_entryfilter_research]]."""
        if direction == Direction.BUY:
            closed_in_favor = float(candle["close"]) > float(candle["open"])
        else:
            closed_in_favor = float(candle["close"]) < float(candle["open"])

        volumes = df_with_emas["tick_volume"].iloc[:-1]
        vol_threshold = float(volumes.quantile(1 / 3))
        vol_actual = float(candle["tick_volume"])
        low_volume = vol_actual < vol_threshold

        # Point 5 from the same research (2026-09-01): 08:00-12:00
        # broker/app time was the worst window (negative in 3 of 4
        # accounts), 00:00-04:00 the best (positive in all 4). The
        # candle's own index is already raw broker/app time -- MT5's
        # copy_rates_from_pos time converted with no offset correction
        # (see bot/data/market_data.get_ohlc) -- no conversion needed.
        app_hour = candle.name.hour
        in_excluded_window = 8 <= app_hour < 12

        # Higher-timeframe trend filter, added 2026-09-02 after
        # scripts/analyze_trend_filter.py's walk-forward-consistent result
        # on the M3 legs (demo1_m3 EMA100: +$57.54 then +$369.96 across
        # the two halves; demo2_m3 EMA50: +$137.64/+$127.02, best cost/
        # benefit ratio found). demo1_m1's own walk-forward result was
        # inconsistent (sign flip / near-zero first half) -- not a proven
        # edge on M1, logging here anyway for symmetry/future recheck.
        # Computed self-contained from df_with_emas["close"] (already-
        # fetched candle history, no new config/pipeline needed) -- causal
        # by construction (ewm() at position -2 only depends on rows
        # before it), same reasoning as compute_emas() itself.
        close_price = float(candle["close"])
        ema50_at_candle = float(df_with_emas["close"].ewm(span=50, adjust=False).mean().iloc[-2])
        ema100_at_candle = float(df_with_emas["close"].ewm(span=100, adjust=False).mean().iloc[-2])
        if direction == Direction.BUY:
            ema50_trend_agree = close_price > ema50_at_candle
            ema100_trend_agree = close_price > ema100_at_candle
        else:
            ema50_trend_agree = close_price < ema50_at_candle
            ema100_trend_agree = close_price < ema100_at_candle

        return {
            # bool()/float() here matter -- comparisons against a pandas
            # .quantile() result are numpy.bool_/numpy.float64, which
            # json.dumps() (used by log_decision) cannot serialize and
            # would crash real logging in production.
            "shadow_closed_in_favor": bool(closed_in_favor),
            "shadow_low_volume": bool(low_volume),
            "shadow_tick_volume": vol_actual,
            "shadow_app_hour": int(app_hour),
            "shadow_in_excluded_window": bool(in_excluded_window),
            "shadow_volume_threshold": round(vol_threshold, 1),
            "shadow_ema50_trend_agree": bool(ema50_trend_agree),
            "shadow_ema100_trend_agree": bool(ema100_trend_agree),
        }

    def _maybe_enter_or_pend(
        self, direction: Direction, price_now: float, ema13_now: float, base_reason: str,
        cross_candle_time_override: pd.Timestamp | None,
        shadow_filter_info: dict | None = None,
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
                shadow_filter_info=shadow_filter_info,
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

        # Candle-based breakeven check (2026-08-31 addition, real incident):
        # the tick-based check in on_tick() only samples price once per
        # tick_poll_interval_seconds (typically 1s) -- a real trade's tick
        # history showed the live bid crossing the trigger for well under a
        # second (~40ms) and being missed entirely by that single snapshot.
        # This catches it retroactively using data already fetched for
        # cross detection (no extra API calls): if the JUST-CLOSED candle's
        # favorable extreme (high for BUY, low for SELL) reached the
        # trigger at any point during that period, arm breakeven now even
        # though the live tick poll missed the exact moment. Runs BEFORE
        # the swap/reversal logic below so an already-armed breakeven stop
        # (== entry price, strictly better than any tightened distance)
        # doesn't get overwritten by the pending-reversal tightening
        # further down -- see that branch's own guard.
        if (
            self.position is not None
            and self.config.breakeven_trigger_usd is not None
            and not self.position.breakeven_armed
        ):
            if self.position.direction == Direction.BUY:
                candle_favorable = float(last_closed["high"]) - self.position.entry_price
            else:
                candle_favorable = self.position.entry_price - float(last_closed["low"])
            if candle_favorable >= self.config.breakeven_trigger_usd:
                self.position.breakeven_armed = True
                self.position.stop_loss = self._breakeven_stop_price(self.position.direction, self.position.entry_price)
                lock_note = f", locking in ${self.config.breakeven_lock_usd:.2f} profit" if self.config.breakeven_lock_usd else ""
                log_decision(
                    self.config.symbol, "breakeven_armed",
                    f"Candle {last_closed_time} range reached ${candle_favorable:.2f} (>= "
                    f"${self.config.breakeven_trigger_usd:.2f} trigger) -> stop-loss moved to "
                    f"{self.position.stop_loss:.2f}{lock_note} (caught via candle high/low, not the live tick poll)",
                )

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
                                    shadow_filter_info=self._shadow_filter_info(direction, last_closed, df_with_emas),
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
                                f"{self.position.direction.value} position keeps running with its stop-loss "
                                f"still tightened from the pending-reversal warning (stays tightened, not "
                                f"restored -- two opposing candles in a row is a real warning sign even "
                                f"without ADX confirming)",
                            )
                            self.pending_reversal_direction = None
                    else:
                        self.pending_reversal_direction = direction
                        # Tighten the HELD position's stop-loss to half its
                        # normal distance the instant the first opposing
                        # candle appears -- real evidence the trade might be
                        # going wrong, even before the 2-candle+ADX bar is
                        # cleared. If the swap later fires, this is moot
                        # (a fresh position with its own full stop opens).
                        # If it's blocked by ADX, the tightened stop STAYS
                        # (explicit user decision 2026-08-24) -- two candles
                        # opposing the trade in a row is a real warning sign
                        # even without ADX confirming. Traced against real
                        # 2026-08-24 data before building: on two real
                        # losing trades this would have roughly halved the
                        # loss; on a trade with two false-alarm warnings
                        # that still won, price never got remotely close to
                        # where the tightened stop would sit, so it
                        # wouldn't have caused an early false exit either.
                        #
                        # SKIPPED if breakeven already armed: a breakeven
                        # stop sits exactly at entry (guaranteed >= $0
                        # outcome), which is strictly better than any
                        # tightened-but-still-losing distance -- moving it
                        # here would undo real protection the trade already
                        # earned (2026-08-31 fix, found while investigating
                        # why a real trade's breakeven never armed).
                        if self.position.breakeven_armed:
                            log_decision(
                                self.config.symbol, "swap_pending_breakeven_kept",
                                f"{direction.value} candle close (ema13={ema13:.2f}, ema21={ema21:.2f}) opposes "
                                f"held {self.position.direction.value} position but not swapped yet -> stop-loss "
                                f"stays at breakeven-armed entry price {self.position.stop_loss:.2f}, NOT "
                                f"tightened to a worse level",
                            )
                        else:
                            tightened_distance = self.config.stop_loss_usd / 2
                            old_stop_loss = self.position.stop_loss
                            self.position.stop_loss = self._compute_stop_loss(
                                self.position.direction, self.position.entry_price, tightened_distance,
                            )
                            # SHADOW-ONLY, 2026-09-01: the earliest real-time
                            # moment this risk becomes visible at all -- log
                            # what an IMMEDIATE swap (demo2's design) would
                            # have executed at RIGHT NOW, so a later trade-by-
                            # trade comparison (scripts/simulate_swap_debounce_cost.py)
                            # doesn't have to reconstruct it after the fact.
                            # Does not change any real behavior -- the
                            # position keeps running exactly as before.
                            shadow_immediate_swap_price = exit_price
                            shadow_immediate_swap_pl_per_unit = (
                                (shadow_immediate_swap_price - self.position.entry_price)
                                if self.position.direction == Direction.BUY
                                else (self.position.entry_price - shadow_immediate_swap_price)
                            )
                            log_decision(
                                self.config.symbol, "swap_pending",
                                f"{direction.value} candle close (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f}) opposes held {self.position.direction.value} position but "
                                f"not swapped yet -> waiting for next candle to confirm before acting; "
                                f"stop-loss tightened to ${tightened_distance:.2f} (was ${self.config.stop_loss_usd:.2f}) "
                                f"at {self.position.stop_loss:.2f} (was {old_stop_loss:.2f})",
                                shadow_immediate_swap_price=shadow_immediate_swap_price,
                                shadow_immediate_swap_pl_per_unit=round(shadow_immediate_swap_pl_per_unit, 2),
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
                        # ADX-momentum entry filter (required ADX rising
                        # vs the previous candle) was tried 2026-08-24/25
                        # and REMOVED 2026-08-27 -- explicit user decision,
                        # backed by real data: automated trace of all 41
                        # real signals it blocked across its 2 days live
                        # (scripts/simulate_blocked_adx_signals.py,
                        # cross-checked against 4 hand-traced signals,
                        # exact match) showed 27 would-be wins (~+$810) vs
                        # 14 would-be losses (~-$720) -- net ~+$90 LOST by
                        # having the filter, not saved. It was blocking
                        # more good trades than bad ones. See
                        # [[project_dual_cross_and_cross_confirmed]] for
                        # the full history if this is ever revisited.
                        opened = self._maybe_enter_or_pend(
                            direction, exit_price, ema13,
                            base_reason=(
                                f"close-confirmed: candle closed with a genuine "
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                            shadow_filter_info=self._shadow_filter_info(direction, last_closed, df_with_emas),
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
                # Breakeven-stop (config.breakeven_trigger_usd, only set on
                # accounts that want it -- explicit user request 2026-08-31,
                # M1 only: once floating profit reaches this many dollars,
                # move the stop-loss to the entry price ONE-WAY (never
                # un-arms) so the trade can never turn into a real loss
                # once this close to take-profit. Checked BEFORE the
                # stop_hit check below so the same tick can act on the
                # freshly-moved stop, same ordering as the pending-reversal
                # tightening in on_new_candle().
                if self.config.breakeven_trigger_usd is not None and not position.breakeven_armed:
                    favorable = (
                        tick.bid - position.entry_price if position.direction == Direction.BUY
                        else position.entry_price - tick.bid
                    )
                    if favorable >= self.config.breakeven_trigger_usd:
                        position.breakeven_armed = True
                        position.stop_loss = self._breakeven_stop_price(position.direction, position.entry_price)
                        lock_note = f", locking in ${self.config.breakeven_lock_usd:.2f} profit" if self.config.breakeven_lock_usd else ""
                        log_decision(
                            self.config.symbol, "breakeven_armed",
                            f"Floating profit reached ${favorable:.2f} (>= ${self.config.breakeven_trigger_usd:.2f} "
                            f"trigger) -> stop-loss moved to {position.stop_loss:.2f}{lock_note}",
                        )

                stop_hit = (
                    (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                    or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
                )
                if stop_hit:
                    # Report the ACTUAL distance that fired, not always the
                    # base config value -- position.stop_loss may have been
                    # tightened to half by a pending-reversal warning (see
                    # the swap_pending branch in on_new_candle()), and the
                    # log must reflect what really happened, not the
                    # account's normal stop size.
                    actual_distance = abs(position.entry_price - position.stop_loss)
                    # Distinct category when the stop that fired was the
                    # breakeven-armed one -- a near-$0 (or, with
                    # breakeven_lock_usd set, a small guaranteed-profit)
                    # exit is a very different real outcome from a genuine
                    # stop-loss hit, and conflating them would mislead
                    # every category-breakdown analysis this project
                    # relies on (see generate_analytics_json.py/
                    # full_strategy_analysis.py's item2_categories).
                    # Checking the armed flag alone (not stop_loss ==
                    # entry_price) is correct now that breakeven_lock_usd
                    # can move the stop away from exact entry -- once
                    # armed, on_new_candle()'s swap_pending guard ensures
                    # nothing else ever moves this stop again.
                    if position.breakeven_armed:
                        lock_note = (
                            f", locked ${self.config.breakeven_lock_usd:.2f} profit"
                            if self.config.breakeven_lock_usd else ", reversed to entry"
                        )
                        events.append(self._close_position(
                            category="breakeven",
                            reason=f"Breakeven-stop hit at {position.stop_loss:.2f} (armed at "
                                   f"+${self.config.breakeven_trigger_usd:.2f} floating profit{lock_note})",
                            exit_price=position.stop_loss,
                        ))
                    else:
                        events.append(self._close_position(
                            category="stop_loss",
                            reason=f"${actual_distance:.2f} stop-loss hit at {position.stop_loss:.2f}",
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
        shadow_filter_info: dict | None = None,
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
            **(shadow_filter_info or {}),
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
