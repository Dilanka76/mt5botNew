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
from datetime import datetime, timedelta
from dataclasses import dataclass

import MetaTrader5 as mt5
import pandas as pd

from bot.config import AppConfig
from bot.daily_loss import COLOMBO, daily_limit_reason
from bot.execution.trade_executor import TradeExecutor
from bot.indicators.consolidation import is_consolidating
from bot.indicators.htf_trend import agrees_with_trend
from bot.logging_setup.logger import log_decision
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.sessions import is_within_session, weekend_flat_due
from bot.timeframes import minutes_for
from bot.strategy.cross_detector import CrossState, Direction
from bot.strategy.state_machine import POSITION_CLOSE_GRACE_PERIOD_SECONDS, TradeState

# How long a just-closed ticket stays exempt from the duplicate guard.
# Generous on purpose: the window only has to outlast the broker's cache
# update, and a ticket we closed can never become a real duplicate.
STALE_CLOSE_WINDOW_SECONDS = 30.0
from bot.strategy.state_machine_dual_cross import ClosedTrade, DualPosition, OpenedTrade

logger = logging.getLogger("bot.strategy.state_machine_dual_cross_confirmed_swap")

# Candles used for the shadow Efficiency Ratio (Kaufman). 20 is the
# standard default and what the research used.
ER_LOOKBACK = 20


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
        # stop_loss_usd may be None: demo2_m3 runs with NO stop from
        # 2026-09-09 at the user's explicit instruction, holding a losing
        # trade until the opposite cross. The guard that used to forbid
        # this is gone, so every read of a stop below must cope with None
        # -- a position simply has no stop until breakeven arms one.
        # A loss then has no floor before the cross arrives, and a weekend
        # gap can open past anything that would have been accepted. That
        # is the accepted trade-off, not an oversight.
        if config.gap_threshold_usd is None:
            raise ValueError(
                "strategy_variant=dual_cross_confirmed_swap requires gap_threshold_usd to be set "
                "(the gap + EMA5-pullback rule on flat entries)."
            )
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        # Set to today's Colombo date the first time the daily loss
        # limit blocks an entry, so it is logged once a day, not once
        # per candle for the rest of the day.
        self._daily_limit_logged_date = None
        self.position: DualPosition | None = None
        # Tickets this engine has just closed, ticket -> time.monotonic().
        # MT5's positions_get() reads the terminal's local cache, which the
        # server updates asynchronously -- so for a few milliseconds after a
        # SUCCESSFUL close the broker still lists the position. The swap
        # closes and re-enters in the same breath, so the duplicate-position
        # guard was reading that stale cache and refusing the reversal.
        # Live, 2026-09-15 22:54:01: closed at .417, re-entry refused at
        # .424. Seven milliseconds. The BUY closed and the SELL never
        # opened -- the swap went flat instead of reversing.
        self.recently_closed: dict[int, float] = {}
        self.pending: PendingSetup | None = None
        self.prev_ema13: float | None = None
        # WARM-UP. A bot may not ENTER until it has been running for one
        # full candle period, so any cross it acts on provably closed while
        # it was watching.
        #
        # live2_m5, 2026-09-16 22:43:14 -- two seconds after startup, on a
        # cross from a candle that had closed at 22:40, before the process
        # existed. MT5's candle cache is briefly one behind at connect, so
        # loop 1 evaluated candle N-2 (prev was None, no entry, prev set to
        # N-2) and loop 1 second later evaluated N-1 against it and called
        # the historical cross a live one. The `prev_ema13 is None` guard
        # only ever blocked the FIRST of those two.
        #
        # The tell is the gap: normal live entries run 2.75-2.87 from EMA13,
        # these ran 10.16 -- ten dollars of the move already gone. Every
        # restart during a session was opening an unplanned trade at a bad
        # price, which is what the user saw twice from the screen and what
        # cost the first live trade.
        #
        # EXITS are deliberately NOT gated. Swapping out of a position on a
        # cross that fired while the bot was down is the safe direction.
        self._started_monotonic = time.monotonic()
        self._warmup_seconds = minutes_for(config.timeframe) * 60
        # See _cross_is_stale(): which candle prev_ema13/21 belong to.
        self._bar = timedelta(minutes=minutes_for(config.timeframe))
        self.prev_candle_time = None
        self.prev_ema21: float | None = None
        self.current_ema5: float | None = None
        self.current_candle_time: pd.Timestamp | None = None
        self.current_htf_trend: float | None = None

    def _warmed_up(self) -> bool:
        """True once a full candle period has passed since startup."""
        return (time.monotonic() - self._started_monotonic) >= self._warmup_seconds

    def _cross_is_stale(self, direction: Direction, when) -> bool:
        """True when the candle this cross is measured against is MORE than
        one candle before `when` -- so the "cross" spans a gap the engine
        was not watching, and must not be entered.

        REAL INCIDENT, demo2, 2026-09-21: the MT5 terminal's Algo Trading
        button was off, so at 16:09 order_send failed (retcode 10027). An
        exception before the end of on_new_candle() leaves prev_ema13/21
        unchanged, so EVERY later candle compared against the pre-16:09
        values and re-detected the same cross -- 148 errors in 30 minutes,
        and the moment the button came back on it would have opened a trade
        on a cross hours old. That is the late, extended entry that carries
        the monster losses (see project_entry_research_2026_09_21).

        The same applies to any failure that outlives a candle: a lost
        connection, a closed market, a margin refusal. Retrying WITHIN the
        same candle is still allowed (the gap is exactly one candle), which
        keeps a genuine transient refusal recoverable.

        ENTRIES ONLY. The exit path never asks this: a position still
        closes on the opposite cross however late the candle is -- the
        same rule as the warm-up guard.
        """
        prev = getattr(self, "prev_candle_time", None)
        bar = getattr(self, "_bar", None)
        if prev is None or bar is None or when is None:
            return False
        try:
            gap = when - prev
        except TypeError:
            return False
        if gap <= bar:
            return False
        log_decision(
            self.config.symbol, "entry_skipped_stale_cross",
            f"{direction.value} cross at {when} not taken: it is measured against the candle "
            f"at {prev}, {gap} earlier -- more than one {self.config.timeframe} candle, so it "
            f"is not a fresh cross (an earlier failure, or a gap in the data, left it behind).",
        )
        return True

    def _active_sessions(self) -> list:
        return self.config.sessions["dual_cross_confirmed_swap"]

    def _compute_stop_loss(self, direction: Direction, entry_price: float) -> float | None:
        distance = self.config.stop_loss_usd
        if distance is None:
            return None          # no stop: the opposite cross is the only exit
        return (
            entry_price - distance if direction == Direction.BUY
            else entry_price + distance
        )

    def _take_profit_for(self, direction: Direction) -> tuple[float, str]:
        """The target for a trade about to open, and why it was chosen.

        demo2_m3, user request 2026-09-09: a trade running WITH the
        higher-timeframe EMA13/21 trend aims further, because a move that
        agrees with the bigger picture is expected to have more room. A
        trade against it keeps the normal target.

        Deliberate properties:
          - It never blocks a trade. Every cross still trades; only the
            target moves. That sidesteps the trap that has sunk about a
            dozen entry filters here, where removing trades during a
            losing stretch always looks profitable.
          - It is decided ONCE, at entry, and the target goes to the
            broker with the order. A later flip on the higher timeframe
            does not move it.
          - An unknown trend takes the normal target, never the bigger
            one.
        """
        base = self.config.take_profit_usd
        bigger = self.config.htf_trend_take_profit_usd
        if bigger is None:
            return base, ""
        if agrees_with_trend(direction.value, self.current_htf_trend):
            return bigger, (f" -- with the {self.config.htf_trend_timeframe} trend "
                            f"(EMA13/21), so aiming ${bigger:.2f} instead of ${base:.2f}")
        known = self.current_htf_trend is not None and self.current_htf_trend == self.current_htf_trend
        return base, (f" -- against the {self.config.htf_trend_timeframe} trend, "
                      f"keeping ${base:.2f}" if known else
                      f" -- {self.config.htf_trend_timeframe} trend not known yet, "
                      f"keeping ${base:.2f}")

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

    def _consolidation(self, candle) -> tuple:
        """(verdict, the numbers to log). True = price is boxed in.

        Read from the candle's own cons_* columns, which main.py and
        scripts/backtest.py both compute -- they MUST stay in step: an
        engine reading a column only one side provides is the 2026-08-21
        fault that disabled a stop-loss. Missing columns give None, and
        None means trade exactly as before.
        """
        overlap = candle.get("cons_overlap") if hasattr(candle, "get") else None
        box_atr = candle.get("cons_box_atr") if hasattr(candle, "get") else None
        verdict = is_consolidating(overlap, box_atr, self.config.consolidation_filter)
        return verdict, {
            # float()/bool() for the same json.dumps reason as the shadow
            # fields below: these arrive as numpy scalars.
            "cons_overlap": None if overlap is None or overlap != overlap else float(overlap),
            "cons_box_atr": None if box_atr is None or box_atr != box_atr else float(box_atr),
            "cons_is_box": verdict,
        }

    def _blocked_by_consolidation(self, direction: Direction, candle, when) -> bool:
        """True only when the filter is enabled, NOT shadow_only, and this
        really is a box. ENTRIES ONLY -- an exit is never blocked, which
        keeps the opposite-cross exit (the real stop) untouched."""
        cfg = self.config.consolidation_filter
        verdict, info = self._consolidation(candle)
        if verdict is not True or cfg is None or cfg.shadow_only:
            return False
        log_decision(
            self.config.symbol, "entry_skipped_consolidation",
            f"{direction.value} cross at {when} not taken: price is boxed in on "
            f"{cfg.timeframe} (candles overlap {info['cons_overlap']:.2f} >= "
            f"{cfg.overlap_min:.2f}, and the range is only {info['cons_box_atr']:.2f} "
            f"<= {cfg.box_atr_max:.2f} ATR tall).",
            **info,
        )
        return True

    def _shadow_filter_info(self, direction: Direction, candle, df_with_emas: pd.DataFrame) -> dict:
        """SHADOW-ONLY, 2026-09-02: ported from the sibling ADX engine
        (state_machine_dual_cross_confirmed_swap_adx.py) so demo2 also
        accumulates forward evidence for the same entry-quality research —
        computes (but never acts on) what the candle-color / tick-volume /
        time-of-day / trend filters would have said about this REAL entry,
        attached to the trade_entered log line. Does NOT change trading
        behavior at all. See [[project_demo3_entryfilter_research]]. Note:
        the swap-debounce shadow field the ADX engine also logs
        (shadow_immediate_swap_price/pl_per_unit) does NOT apply here —
        this engine already swaps immediately, there is no debounce to
        shadow-compare against."""
        if direction == Direction.BUY:
            closed_in_favor = float(candle["close"]) > float(candle["open"])
        else:
            closed_in_favor = float(candle["close"]) < float(candle["open"])

        volumes = df_with_emas["tick_volume"].iloc[:-1]
        vol_threshold = float(volumes.quantile(1 / 3))
        vol_actual = float(candle["tick_volume"])
        low_volume = vol_actual < vol_threshold

        # Candle's own index is already raw broker/app time -- no
        # conversion needed (see bot/data/market_data.get_ohlc).
        app_hour = candle.name.hour
        in_excluded_window = 8 <= app_hour < 12

        # Higher-timeframe trend filter, added 2026-09-02 after
        # scripts/analyze_trend_filter.py's walk-forward-consistent result
        # on demo2_m3 (EMA50: +$137.64 first half, +$127.02 second half,
        # only 18% of trades skipped -- the best cost/benefit ratio found
        # so far). Computed self-contained from df_with_emas["close"]
        # (already-fetched candle history, no new config/pipeline needed)
        # -- causal by construction (ewm() at position -2 only depends on
        # rows before it), same reasoning as compute_emas() itself. NOT
        # yet a real rule anywhere -- demo1_m1/demo2_m1 walk-forward
        # results were inconsistent/negative, so this is being logged
        # everywhere but only expected to matter on the M3 legs.
        close_price = float(candle["close"])
        ema50_at_candle = float(df_with_emas["close"].ewm(span=50, adjust=False).mean().iloc[-2])
        ema100_at_candle = float(df_with_emas["close"].ewm(span=100, adjust=False).mean().iloc[-2])
        if direction == Direction.BUY:
            ema50_trend_agree = close_price > ema50_at_candle
            ema100_trend_agree = close_price > ema100_at_candle
        else:
            ema50_trend_agree = close_price < ema50_at_candle
            ema100_trend_agree = close_price < ema100_at_candle


        # Kaufman Efficiency Ratio, added 2026-09-05. STANDARD indicator
        # (the core of his Adaptive Moving Average), not invented here:
        # net directional move over the last 20 candles divided by the
        # total distance travelled. Near 1 = clean trend, near 0 = chop.
        # Scale-free 0..1, so a fixed threshold means the same thing in
        # all conditions -- unlike tick volume, which needs a rolling
        # percentile.
        #
        # SHADOW ONLY. Historical evidence is the strongest this project
        # has produced -- scripts/analyze_er_vs_adx.py, using a measure
        # immune to the losing-period artifact, found high ER beat low ER
        # in BOTH halves on BOTH M3 accounts (demo1_m3 +$23.16 vs -$11.90
        # per trade; demo2_m3 +$16.57 vs -$12.76) and REVERSED on both M1
        # accounts. But it is all historical: three filters have already
        # behaved differently live than in backtest (colour, volume, and
        # the EMA50/100 trend filter, which looked walk-forward-consistent
        # and then hurt on all four accounts). So this logs only.
        #
        # Also confirmed NOT redundant with the ADX demo1 already
        # computes -- correlation just +0.20 to +0.31.
        #
        # Two definitions, because the standard one is blind to what
        # happens INSIDE a candle (a bar that spikes $6 up, crashes $12
        # down and closes flat reads as calm). Stops are hit
        # intra-candle, so the true-range version may predict better.
        # Both causal: computed on closed candles up to index -2 only.
        er_window = df_with_emas.iloc[-(ER_LOOKBACK + 2):-1]
        er_close_only = er_true_range = None
        if len(er_window) >= ER_LOOKBACK + 1:
            er_closes = er_window["close"]
            er_net = abs(float(er_closes.iloc[-1]) - float(er_closes.iloc[0]))
            er_total_close = float(er_closes.diff().abs().sum())
            er_prev_close = er_closes.shift(1)
            er_tr = pd.concat([
                er_window["high"] - er_window["low"],
                (er_window["high"] - er_prev_close).abs(),
                (er_window["low"] - er_prev_close).abs(),
            ], axis=1).max(axis=1)
            er_total_tr = float(er_tr.iloc[1:].sum())
            if er_total_close > 0:
                er_close_only = er_net / er_total_close
            if er_total_tr > 0:
                er_true_range = er_net / er_total_tr

        return {
            # Consolidation, 2026-09-20: written on EVERY entry, on every
            # account, whether or not the filter is acting. live2 runs it
            # shadow_only and keeps trading as it does today -- these
            # three fields are what its forward evidence is made of.
            **self._consolidation(candle)[1],
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
            "shadow_er_close": round(er_close_only, 4) if er_close_only is not None else None,
            "shadow_er_truerange": round(er_true_range, 4) if er_true_range is not None else None,
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

    def _self_heal_desync(self) -> None:
        """Adopt a broker position the engine has forgotten.

        reconcile_on_startup() does this once, at launch. Nothing did it
        mid-run, so a desync lasted until the process restarted. On
        2026-09-14 demo1_m3 went IDLE at 03:00:42 holding an open trade and
        was still IDLE at 05:33 -- two and a half hours in which the trade
        had no take-profit (the runner had just removed it), no software
        stop, and nobody watching it run +$13.19 back to a loss.

        Called once per candle rather than per tick, which gives a close
        that is genuinely in flight time to settle at the broker before
        this could mistake it for a desync.
        """
        if self.position is not None or self.config.execution.mode == "shadow":
            return
        try:
            broker_position = self.executor.get_open_position()
        except Exception:  # noqa: BLE001 - a read failure must not kill the loop
            return
        if broker_position is None:
            return
        direction = Direction.BUY if broker_position.type == mt5.ORDER_TYPE_BUY else Direction.SELL
        self.position = DualPosition(
            direction=direction, ticket=broker_position.ticket,
            entry_price=broker_position.price_open, take_profit=broker_position.tp,
            stop_loss=self._compute_stop_loss(direction, broker_position.price_open),
            cross_candle_time=None, is_concurrent_entry=False, validated=True,
        )
        self._update_state()
        log_decision(
            self.config.symbol, "position_desync_healed",
            f"Engine was flat but the broker held {direction.value} ticket "
            f"{broker_position.ticket} -- adopted it. The stop is rebuilt at the "
            f"configured distance, so any breakeven or runner lock this trade had "
            f"earned is GONE; the runner will re-arm if price is still past the "
            f"arm point. Something cleared the position without closing it -- "
            f"check the log above for an exception.",
            ticket=broker_position.ticket, entry=broker_position.price_open,
            tp=broker_position.tp,
        )

    def on_new_candle(self, df_with_emas: pd.DataFrame) -> list[OpenedTrade | ClosedTrade]:
        events: list[OpenedTrade | ClosedTrade] = []
        self._self_heal_desync()
        last_closed = df_with_emas.iloc[-2]
        last_closed_time = last_closed.name
        # Read from THIS candle, before any entry runs, so a trade opened
        # below uses the trend as of the candle that triggered it. Set at
        # the end of the method (like prev_ema13) it would be one candle
        # stale for the very entry it is meant to size.
        self.current_htf_trend = (
            float(last_closed["htf_trend"]) if "htf_trend" in last_closed.index else None
        )
        ema5 = float(last_closed["ema5"])
        ema13 = float(last_closed["ema13"])
        ema21 = float(last_closed["ema21"])
        exit_price = float(last_closed["close"])

        # Candle-based breakeven check (2026-08-31 addition, real incident
        # -- see the sibling ADX engine's identical block for the full
        # writeup): the tick-based check in on_tick() only samples price
        # once per tick_poll_interval_seconds, which can miss a real but
        # very brief (sub-second) touch of the trigger. This catches it
        # retroactively using the just-closed candle's high/low, already
        # fetched for cross detection -- no extra API calls. This engine
        # has no pending-reversal stop-tightening to guard against
        # overwriting (see module docstring), so no further guard needed.
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
                    if not self._warmed_up():
                        # See the warm-up note in __init__. The close above
                        # still happened -- exiting on a cross that fired
                        # while the bot was down is the safe direction. Only
                        # the re-entry is withheld, because that cross is
                        # history and the price has moved on.
                        log_decision(
                            self.config.symbol, "entry_skipped_warmup",
                            f"{direction.value} cross at {last_closed_time} ignored for entry: "
                            f"the bot has been running less than one {self.config.timeframe} "
                            f"candle, so this cross closed before it was watching.",
                        )
                    elif not is_within_session(self._active_sessions()):
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} confirmed cross at {last_closed_time}, no session open",
                        )
                    elif self._cross_is_stale(direction, last_closed_time):
                        pass      # logged inside; a cross older than one candle is history
                    elif self._blocked_by_consolidation(direction, last_closed, last_closed_time):
                        pass      # logged inside; the entry is withheld, exits are not
                    else:
                        opened = self._enter(
                            direction,
                            reason=(
                                f"close-confirmed (immediate reversal): candle closed with a genuine "
                                f"{prev_state.value}->{new_state.value} cross (ema13={ema13:.2f}, "
                                f"ema21={ema21:.2f})"
                            ),
                            cross_candle_time_override=last_closed_time,
                            shadow_filter_info=self._shadow_filter_info(direction, last_closed, df_with_emas),
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

                    if not self._warmed_up():
                        # See the warm-up note in __init__. The close above
                        # still happened -- exiting on a cross that fired
                        # while the bot was down is the safe direction. Only
                        # the fresh entry is withheld, because that cross is
                        # history and the price has moved on.
                        log_decision(
                            self.config.symbol, "entry_skipped_warmup",
                            f"{direction.value} cross at {last_closed_time} ignored for entry: "
                            f"the bot has been running less than one {self.config.timeframe} "
                            f"candle, so this cross closed before it was watching.",
                        )
                    elif not is_within_session(self._active_sessions()):
                        log_decision(
                            self.config.symbol, "cross_ignored_outside_session",
                            f"{direction.value} confirmed cross at {last_closed_time}, no session open",
                        )
                    elif self._cross_is_stale(direction, last_closed_time):
                        pass      # logged inside; a cross older than one candle is history
                    elif self._blocked_by_consolidation(direction, last_closed, last_closed_time):
                        pass      # logged inside; the entry is withheld, exits are not
                    else:
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
        self.prev_candle_time = last_closed_time
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
        # ONE attempt per setup: cleared BEFORE the order, not after. It
        # used to be cleared only once _enter() returned, so a refused order
        # (2026-09-22, demo2: Algo Trading off, retcode 10027) left the setup
        # in place and on_tick() retried it on EVERY tick -- and the moment
        # trading resumed it opened on a setup hours old, the same late
        # entry as the stale cross. A cleared setup cannot fire late.
        self.pending = None
        opened = self._enter(
            pending.direction,
            reason=f"EMA5 touch at {tick.bid:.2f} for pending {pending.direction.value} setup ({pending.reason})",
            cross_candle_time_override=pending.cross_candle_time,
        )
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
            # BE FLAT FOR THE WEEKEND. Checked before every other exit so a
            # position cannot survive into a weekend on a technicality. A
            # stop only fires when ticks arrive and none arrive over a
            # weekend, so a gap reopens past it and the bot closes at
            # whatever price exists -- not at the stop.
            if weekend_flat_due(self.config.weekend_flat_utc):
                favorable = (
                    tick.bid - position.entry_price if position.direction == Direction.BUY
                    else position.entry_price - tick.bid
                )
                events.append(self._close_position(
                    category="weekend_flat",
                    reason=(f"weekend close: flat by {self.config.weekend_flat_utc} UTC Friday, "
                            f"closing at {favorable:+.2f} rather than carry the position over "
                            f"the weekend, where a gap can open past the stop"),
                    exit_price=tick.bid,
                ))
                return events
            if time.monotonic() - position.opened_monotonic >= POSITION_CLOSE_GRACE_PERIOD_SECONDS:
                # Breakeven-stop (config.breakeven_trigger_usd, only set on
                # accounts that want it -- explicit user request 2026-08-31,
                # M1 only: once floating profit reaches this many dollars,
                # move the stop-loss to the entry price ONE-WAY (never
                # un-arms) so the trade can never turn into a real loss
                # once this close to take-profit. Checked BEFORE the
                # stop_hit check below so the same tick can act on the
                # freshly-moved stop.
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

                stop_hit = position.stop_loss is not None and (
                    (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                    or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
                )
                if stop_hit:
                    # Distinct category when the stop that fired was the
                    # breakeven-armed one (stop_loss == entry_price) -- a
                    # $0 exit is a very different real outcome from a
                    # genuine stop-loss hit, and the old reason text
                    # (always citing config.stop_loss_usd) would be wrong
                    # here since the real distance that fired is $0, not
                    # the account's normal stop size.
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
                            reason=f"${self.config.stop_loss_usd:.2f} stop-loss hit at {position.stop_loss:.2f}"
                                   if self.config.stop_loss_usd is not None else
                                   f"stop hit at {position.stop_loss:.2f}",
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
        # Daily loss limit (config.daily_loss_limit_usd). Checked HERE, in
        # the single funnel every entry path goes through, rather than at
        # each of the three call sites -- one guard that cannot be missed
        # when a fourth path is added later. Blocks NEW entries only; an
        # open position keeps its stop, take-profit and swap exit.
        blocked = daily_limit_reason(
            self.config.logging.log_dir, self.config.account, self.config.daily_loss_limit_usd,
        )
        if blocked is not None:
            today = datetime.now(COLOMBO).date()
            if self._daily_limit_logged_date != today:
                # Once per day, not once per candle -- this is hit on every
                # signal for the rest of the day.
                self._daily_limit_logged_date = today
                log_decision(self.config.symbol, "daily_loss_limit_hit", blocked)
            return None

        # Second line of defence against a DUPLICATE POSITION. The OS
        # mutex in main.py stops a duplicate PROCESS; this stops a
        # duplicate POSITION even if one somehow runs. On 2026-09-08 two
        # processes on demo2_m1 each opened a SELL one second apart --
        # 0.12 lots became 0.24 on an account meant to hold one.
        #
        # This engine only ever holds one position, so if it believes it
        # is flat while the broker shows one carrying our magic number,
        # something else opened it. Adding a second would double the risk
        # of the trade, which on a real account is the whole danger.
        #
        # Failing to READ the broker does not block the entry: the mutex
        # is the primary guard, and refusing to trade on every transient
        # query error would be its own kind of outage. The refusal is
        # logged loudly so a false block (e.g. a swap re-entry racing the
        # close it just sent) would be visible rather than silent.
        try:
            already_open = self.executor.get_open_positions()
        except Exception:  # noqa: BLE001 - a read failure must not kill the loop
            already_open = []

        # Tell a STALE READ apart from a REAL duplicate. A position we closed
        # moments ago that the broker has not dropped from its cache yet is
        # the first; anything else -- another process, a manual trade -- is
        # the second, and still refused. Comparing TICKETS is what separates
        # them: a duplicate opened by something else carries a ticket this
        # engine has never seen.
        now_mono = time.monotonic()
        self.recently_closed = {t: ts for t, ts in self.recently_closed.items()
                                if now_mono - ts < STALE_CLOSE_WINDOW_SECONDS}
        stale = [p for p in already_open if p.ticket in self.recently_closed]
        already_open = [p for p in already_open if p.ticket not in self.recently_closed]
        if stale and not already_open:
            log_decision(
                self.config.symbol, "entry_allowed_stale_close",
                f"Broker still lists ticket(s) "
                f"{', '.join(str(p.ticket) for p in stale)}, which this engine closed "
                f"moments ago -- a stale cache read, not a duplicate. Proceeding with "
                f"the {direction.value} entry.",
            )
        if already_open:
            log_decision(
                self.config.symbol, "entry_blocked_existing_position",
                f"{direction.value} entry REFUSED: this engine believes it is flat but the "
                f"broker already shows {len(already_open)} position(s) with magic "
                f"{self.config.execution.magic_number} "
                f"(tickets {', '.join(str(p.ticket) for p in already_open)}). "
                f"Opening another would double the position size. Check for a duplicate bot.",
            )
            return None

        if weekend_flat_due(self.config.weekend_flat_utc):
            log_decision(
                self.config.symbol, "entry_blocked_weekend",
                f"{direction.value} entry refused: flat by {self.config.weekend_flat_utc} UTC "
                f"Friday. A trade opened now could not be closed before the market shuts.",
            )
            return None

        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)
        take_profit_usd, trend_note = self._take_profit_for(direction)
        # The backstop goes in the SAME request as the order. A follow-up
        # modify would leave a window with no stop, and surviving the bot
        # dying is the entire point of it.
        result = self.executor.open_market_order(
            direction, lots, take_profit_usd, self.config.broker_backstop_usd)

        cross_candle_time = (
            cross_candle_time_override if cross_candle_time_override is not None else self.current_candle_time
        )
        self.position = DualPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self._compute_stop_loss(direction, result.price),
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, validated=True,
        )

        log_decision(
            self.config.symbol, "trade_entered", reason + trend_note,
            direction=direction.value, lots=lots, entry=result.price, tp=result.take_profit,
            stop_loss=self.position.stop_loss, balance=balance, pre_validated=True,
            # Recorded on EVERY entry, including accounts with no
            # higher-timeframe rule, so the question "do trend-aligned
            # trades actually run further?" can be answered from the logs
            # later without rebuilding anything. The rule went live
            # untested at the user's request; this is what makes it
            # measurable afterwards.
            htf_trend=self.current_htf_trend,
            htf_aligned=agrees_with_trend(direction.value, self.current_htf_trend),
            target_usd=take_profit_usd,
            **(shadow_filter_info or {}),
        )

        return OpenedTrade(
            direction=direction, ticket=result.ticket, entry_price=result.price,
            take_profit=result.take_profit, stop_loss=self.position.stop_loss,
            cross_candle_time=cross_candle_time, is_concurrent_entry=False, is_fallback_entry=True,
        )

    def _close_position(self, category: str, reason: str, exit_price: float) -> ClosedTrade:
        position = self.position
        # Close FIRST, forget SECOND. The reverse order orphaned a real
        # trade on demo1_m3 (2026-09-14 03:00:42): close_position() raised,
        # self.position was already None, so the engine went IDLE while the
        # broker still held the position -- and because the TP-runner had
        # deleted the broker take-profit one second earlier, that trade sat
        # for 2.5 hours with no target, no software stop and nothing
        # managing it, running +$13.19 to a loss unattended. Nothing was
        # even logged, because the log_decision below never ran either.
        #
        # Clearing state only after the broker call succeeds means a failed
        # close leaves the position intact and the next tick simply tries
        # again. If the close DID reach the broker despite raising, the
        # live_tickets reconciliation closes it out properly instead.
        self.executor.close_position(position.ticket)
        # Remember it BEFORE clearing state: the re-entry that follows a swap
        # runs microseconds later and must not mistake the broker's stale
        # cache entry for a duplicate position.
        if position.ticket is not None:
            self.recently_closed[position.ticket] = time.monotonic()
        self.position = None
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
