"""The EMA-cross scalping state machine.

Entry (unchanged mechanics, open-price gap formula — see
docs/STRATEGY_PROPOSED_OPEN_GAP.md):
  A cross is confirmed only on a closed candle. Gap is measured using
  that cross candle's own OPEN price against EMA13. Under the threshold
  -> enter immediately. At or over it -> wait for a live tick to touch
  EMA5, then enter.

Exit — this is the newer design, replacing the old "any opposite cross
instantly closes the trade" rule:
  An open position is watched every candle: does EMA13/21 still match
  the direction it was opened in?
    - Yes -> nothing happens, keep holding.
    - No ("invalid") -> two things now race each other, both checked
      every candle/tick, whichever completes first closes the position:
        (a) EMA5 vs EMA9 confirms the reversal (a faster pair, checked
            only once a position has already gone invalid) -> close as
            a stop-loss.
        (b) A brand-new, independently confirmed opposite cross
            actually completes its own full entry (same gap-check /
            immediate-or-wait rule as any entry) -> that new trade
            opening automatically closes the old one.
  If EMA13/21 flips back to match the original direction before either
  of those completes, the position simply goes back to "valid" and
  keeps running — no exit.
  Take-profit is checked unconditionally, independent of all the above.
  stop_loss_usd / breakeven_trigger_usd remain supported (dormant,
  optional, unused by the finalized design) for any account that still
  wants them; the new design's accounts simply leave both unset.

Because an invalidated position doesn't close immediately, `open_position`
and `pending` (a fresh independent entry attempt in the new direction)
can be set at the same time — this is intentional: the account can
briefly hold the old (invalid, being watched) position while a new one
is still being decided. `state` is a best-effort coarse summary for
logging only; the real source of truth is `open_position`/`pending`.

A cross that occurs outside a configured session is ignored entirely: no
setup is created, and the bot simply waits for the next fresh cross once a
session opens (crosses aren't "banked" across a session boundary). If a
pending setup's EMA5 touch would trigger an entry after the session has
since closed, that entry is skipped too.
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
    invalid: bool = False  # True once EMA13/21 no longer matches `direction` — see module docstring


class EMAScalpEngine:
    def __init__(self, config: AppConfig, connector: MT5Connector, executor: TradeExecutor):
        self.config = config
        self.connector = connector
        self.executor = executor

        self.state = TradeState.IDLE
        self.pending: PendingSetup | None = None
        self.open_position: OpenPosition | None = None
        self.current_ema5: float | None = None
        # Set every time a position closes, to the same short category the
        # reason string starts with — read by bot.backtest.runner instead of
        # re-deriving "why did it close" from raw prices, which is exactly
        # the kind of guess that drifted out of sync once already (see
        # scripts/verify_cross_gap_openprice.py's fixed bug). Not used by
        # live trading itself, only by the backtest driver.
        self.last_close_reason: str | None = None
        # The previous CLOSED candle's real EMA13/21 — the only legitimate
        # base for the early-entry check below (see _check_early_entry).
        # Updated at the end of every on_new_candle() call. None until the
        # first candle has been processed.
        self.prev_ema13: float | None = None
        self.prev_ema21: float | None = None

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
            # A reconciled position has no tick/candle history from this
            # run, so it starts unarmed and valid regardless of its actual
            # state at reconcile time — self-heals on the next candle.
            breakeven_armed=False,
            invalid=False,
        )
        self._update_state()
        logger.info(
            "Reconciled existing open position on startup: ticket=%s direction=%s entry=%.2f tp=%.2f",
            position.ticket, direction.value, position.price_open, position.tp,
        )

    def _update_state(self) -> None:
        """Best-effort coarse summary for logging/status only — real
        control flow reads open_position/pending directly, since those two
        can now both be set at once (an invalidated position being watched
        while a new one is independently being decided)."""
        if self.open_position is not None:
            self.state = TradeState.IN_POSITION
        elif self.pending is not None:
            self.state = TradeState.PENDING_ENTRY
        else:
            self.state = TradeState.IDLE

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
        last_closed = df_with_emas.iloc[-2]
        self.current_ema5 = float(last_closed["ema5"])

        # Recorded unconditionally, before any branching below, so it's
        # always this candle's real, final EMA13/21 — the only legitimate
        # base for the next candle's early-entry check (_check_early_entry).
        self.prev_ema13 = float(last_closed["ema13"])
        self.prev_ema21 = float(last_closed["ema21"])

        # Re-validate any open position against THIS candle's EMA13/21
        # state, independent of whether a fresh cross fires below — this
        # is what lets a position go invalid, come back to valid, or get
        # closed via the EMA5/EMA9 fast path, on every single candle.
        self._recheck_position_validity(last_closed)

        event = detect_cross(df_with_emas)
        if event is None:
            self._update_state()
            return

        symbol = self.config.symbol

        if self.open_position is not None and event.direction == self.open_position.direction:
            # This fresh cross just re-confirms the direction we already
            # hold — _recheck_position_validity() above already marked it
            # valid again if it had gone invalid. Nothing further to do;
            # in particular, do NOT treat this as a new entry opportunity.
            self._update_state()
            return

        # A cross always invalidates any pending setup (it can only be the
        # opposite direction of whatever we were pending, by definition of
        # what a "cross" is) — independent of whatever open_position is
        # doing above.
        if self.pending is not None:
            log_decision(
                symbol,
                "setup_invalidated",
                f"opposite cross before EMA5 touch (was waiting {self.pending.direction.value}, "
                f"gap was {self.pending.gap:.2f})",
            )
            self.pending = None

        if not is_within_session(self._active_sessions()):
            log_decision(
                symbol,
                "cross_ignored_outside_session",
                f"{event.direction.value} cross at {event.candle_time}, no session open",
            )
            self._update_state()
            return

        gap = calculate_gap(event)
        self._decide_entry(event, gap)
        self._update_state()

    def _recheck_position_validity(self, last_closed: pd.Series) -> None:
        """Every candle close: does EMA13/21 still match the open position's
        direction? If it just flipped away, mark it invalid (armed for the
        EMA5/EMA9 fast-exit check below, but NOT closed yet). If it was
        already invalid and has now flipped back to match, clear the flag —
        a false alarm, keep holding normally. If it's already invalid and
        EMA5/EMA9 now confirm the reversal, close it as a stop-loss."""
        position = self.open_position
        if position is None:
            return

        ema13, ema21 = float(last_closed["ema13"]), float(last_closed["ema21"])
        if ema13 == ema21:
            return  # indeterminate candle — extremely rare, skip this check for now

        matches = (
            (position.direction == Direction.BUY and ema13 > ema21)
            or (position.direction == Direction.SELL and ema13 < ema21)
        )

        if matches:
            if position.invalid:
                position.invalid = False
                log_decision(
                    self.config.symbol,
                    "position_revalidated",
                    f"EMA13/21 flipped back to match the {position.direction.value} position — false alarm, keep holding",
                    ticket=position.ticket,
                )
            return

        if not position.invalid:
            position.invalid = True
            log_decision(
                self.config.symbol,
                "position_invalidated",
                f"EMA13/21 no longer matches the {position.direction.value} position — watching EMA5/EMA9 for confirmation",
                ticket=position.ticket,
            )
            return

        # Already invalid — check the faster EMA5/EMA9 pair for confirmation.
        ema5, ema9 = float(last_closed["ema5"]), float(last_closed["ema9"])
        if ema5 == ema9:
            return
        reversal_confirmed = (
            (position.direction == Direction.BUY and ema5 < ema9)
            or (position.direction == Direction.SELL and ema5 > ema9)
        )
        if reversal_confirmed:
            self._close_open_position(
                reason=f"EMA5/EMA9 confirmed reversal (was {position.direction.value}, invalid since EMA13/21 flipped)",
                category="ema59_reversal",
            )

    def _decide_entry(self, event: CrossEvent, gap: float) -> None:
        """Gap-threshold rule: small gap enters immediately, large gap waits
        for an EMA5 touch. Overridden by EMA5OnlyEngine (state_machine_ema5_only.py)
        to always wait for the touch, ignoring the gap entirely."""
        symbol = self.config.symbol
        if gap < self.config.gap_threshold_usd:
            self._enter(event.direction, reason=f"{event.direction.value} cross, gap={gap:.2f} < threshold, immediate entry")
        else:
            self.pending = PendingSetup(direction=event.direction, cross_event=event, gap=gap)
            log_decision(
                symbol,
                "setup_pending",
                f"{event.direction.value} cross, gap={gap:.2f} >= threshold, waiting for EMA5 touch",
            )

    def on_tick(self, tick) -> None:
        """Call frequently (e.g. every ~1s) with the latest tick. pending and
        open_position are independent now (see module docstring) — both get
        checked when both are set, not just one or the other."""
        self._reject_manual_positions(source="tick")
        if self.pending is not None and self.current_ema5 is not None:
            self._check_ema5_touch(tick)
        if self.open_position is not None:
            self._check_position_closed(tick)
        if self.open_position is None and self.pending is None:
            self._check_early_entry(tick)
        self._update_state()

    def _check_early_entry(self, tick) -> None:
        """Optional (early_entry_threshold_usd, default off): while idle,
        recomputes a provisional EMA13/21 on every tick using ONLY the
        current tick's real price blended with the previous CLOSED
        candle's real EMA13/21 — never the current, still-forming
        candle's eventual close, which would not genuinely be known yet
        (see docs/STRATEGY_PROPOSED_OPEN_GAP.md for why that distinction
        matters). If the provisional pair is within threshold, evaluates
        the same immediate-entry gap check any confirmed cross would —
        entering right away only if the gap still qualifies as small; a
        wide gap here does nothing early and leaves the normal
        end-of-candle confirmation to handle it as always."""
        if self.config.early_entry_threshold_usd is None:
            return
        if self.prev_ema13 is None or self.prev_ema21 is None:
            return

        k_mid = 2 / (self.config.ema_periods.mid + 1)
        k_slow = 2 / (self.config.ema_periods.slow + 1)
        prov_ema13 = tick.bid * k_mid + self.prev_ema13 * (1 - k_mid)
        prov_ema21 = tick.bid * k_slow + self.prev_ema21 * (1 - k_slow)

        if abs(prov_ema13 - prov_ema21) > self.config.early_entry_threshold_usd:
            return

        # Direction: approaching from whichever side the last REAL,
        # already-closed candle was on — the only side a genuine new
        # cross could be forming from.
        if self.prev_ema13 < self.prev_ema21:
            direction = Direction.BUY
            gap = tick.bid - prov_ema13
        else:
            direction = Direction.SELL
            gap = prov_ema13 - tick.bid

        if gap >= self.config.gap_threshold_usd:
            return  # wide gap — do nothing early, let normal confirmation handle it

        if not is_within_session(self._active_sessions()):
            return  # same session gating every other entry path respects

        self._enter(
            direction,
            reason=(
                f"EARLY entry: provisional EMA13/21 within "
                f"${self.config.early_entry_threshold_usd:.2f} (actual ${abs(prov_ema13 - prov_ema21):.2f}), "
                f"gap={gap:.2f} < threshold, before candle close"
            ),
        )

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

        self.pending = None

    def _check_position_closed(self, tick) -> None:
        """Detects a broker-side TP fill (or, in shadow mode, simulates one),
        plus the still-supported (but by default unused) stop_loss_usd and
        breakeven_trigger_usd checks. The EMA5/EMA9 reversal check lives in
        _recheck_position_validity() instead, since it's evaluated once per
        candle close, not per tick.

        Skipped for a short grace period right after opening: a live broker
        can take a moment to make a just-filled order visible via
        positions_get(), and without this grace period that lag gets
        misread as an instant TP fill (see POSITION_CLOSE_GRACE_PERIOD_SECONDS)."""
        position = self.open_position

        if time.monotonic() - position.opened_monotonic < POSITION_CLOSE_GRACE_PERIOD_SECONDS:
            return

        # Bot-managed stop-loss: still supported for any account that wants
        # it, dormant when stop_loss_usd is unset (the finalized design
        # leaves it unset — take-profit + the EMA5/EMA9 race are the only
        # exits). Checked ahead of take-profit, same as always.
        if position.stop_loss is not None:
            stop_hit = (
                (position.direction == Direction.BUY and tick.bid <= position.stop_loss)
                or (position.direction == Direction.SELL and tick.bid >= position.stop_loss)
            )
            if stop_hit:
                self._close_open_position(reason=f"stop-loss hit at {position.stop_loss:.2f}", category="stop_loss")
                return

        # Bot-managed breakeven-stop: same "still supported, dormant by
        # default" status as stop-loss above.
        if self.config.breakeven_trigger_usd is not None:
            sign = 1 if position.direction == Direction.BUY else -1
            favorable = (tick.bid - position.entry_price) * sign
            if not position.breakeven_armed and favorable >= self.config.breakeven_trigger_usd:
                position.breakeven_armed = True
            if position.breakeven_armed and favorable <= 0:
                self._close_open_position(
                    reason=f"breakeven-stop (armed at +{self.config.breakeven_trigger_usd:.2f}, returned to entry)",
                    category="breakeven",
                )
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
                self.last_close_reason = "take_profit"
            return

        if self.executor.get_open_position() is None:
            log_decision(
                self.config.symbol,
                "trade_closed_tp",
                "position no longer open on broker (TP fill or external close)",
            )
            self.open_position = None
            self.last_close_reason = "take_profit"

    def _enter(self, direction: Direction, reason: str) -> None:
        # If a position is already open at this point, it can only be an
        # invalidated one from the opposite direction (see on_new_candle —
        # a same-direction cross never reaches _decide_entry/_enter). A
        # genuinely new, independently confirmed cross automatically closes
        # it here, right as the new one opens — the "normal path" from
        # docs/STRATEGY_PROPOSED_OPEN_GAP.md.
        if self.open_position is not None:
            self._close_open_position(
                reason=f"new confirmed {direction.value} cross took over (was {self.open_position.direction.value}, invalid)",
                category="new_cross_confirmed",
            )

        balance = self.connector.account_info().balance
        lots = calculate_lots(balance, self.config.position_sizing)

        result = self.executor.open_market_order(direction, lots, self.config.take_profit_usd)

        self.open_position = OpenPosition(
            direction=direction, ticket=result.ticket, entry_price=result.price, take_profit=result.take_profit,
            stop_loss=self._compute_stop_loss(direction, result.price),
        )

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

    def _close_open_position(self, reason: str, category: str) -> None:
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
        self.last_close_reason = category
