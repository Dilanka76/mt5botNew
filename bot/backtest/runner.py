"""Replays historical price data through the exact same, unmodified live
strategy engine to produce a much larger sample of hypothetical trades
than the live/demo accounts have accumulated so far. Three engines share
this one driver, with genuinely different recording mechanics:

- gap_threshold (EMAScalpEngine, docs/STRATEGY_CURRENT.md): single
  open_position slot, diffed by object identity before/after each
  on_tick()/on_new_candle() call; exit classification reads
  engine.last_close_reason directly rather than re-deriving "why did it
  close" from raw prices (this replaced an earlier version that tried to
  reconstruct the reason from stop_loss/breakeven price thresholds — a
  threshold-guessing approach that drifted out of sync once already, see
  scripts/verify_cross_gap_openprice.py's fixed bug, same class of issue).
- dual_cross (DualCrossEngine, docs/STRATEGY_DUAL_CROSS_SPEC.md): up to 2
  simultaneous positions, keyed by direction — no diffing at all, since
  on_tick()/on_new_candle() return explicit OpenedTrade/ClosedTrade event
  lists directly (see state_machine_dual_cross.py's module docstring).
- cross_confirmed (CrossConfirmedEngine, state_machine_cross_confirmed.py):
  a dual_cross variant built to isolate one question — entries ONLY on an
  already-confirmed close-based cross, no tick-based tolerance entry at
  all. At most one position at a time. Reuses dual_cross's event types and
  the same event-consuming recording path (EVENT_BASED_VARIANTS below).
- cross_confirmed_adaptive_tp (CrossConfirmedAdaptiveTPEngine,
  state_machine_cross_confirmed_adaptive_tp.py): identical entry mechanism
  to cross_confirmed; only the take-profit distance differs, computed per
  trade from the confirming candle's own close-open range instead of a
  fixed take_profit_usd. Also event-based, same recording path.

This is an approximation, not ground truth, UNLESS a `tick_provider` is
passed in (see run_backtest()'s docstring) — then PHASE 1 below replays
real historical ticks instead of the 2-point approximation described
next. Without one (the default):
- No real historical tick data — each 1-minute candle's own high/low are
  used as two synthetic ticks, in candle-direction order (close>=open ->
  low then high, else high then low). If a single bar's range spans both
  a profit target AND a stop-loss/EMA5-9-confirmation, that ordering
  determines the outcome. Verified via scripts/inspect_ticks.py against
  real tick history that this can genuinely miss a real, qualifying
  tick-based entry whose crossing zone falls strictly between a candle's
  low and high rather than at either extreme — not just a theoretical
  gap, a confirmed real-world case (2026-08-10 08:33 UTC, XAUUSDp).
- Spread is synthesized from each candle's own MT5-reported spread
  (points) and the symbol's point size, not the real historical spread
  at that instant.
- decisions.jsonl gets real "now" timestamps during a backtest run, not
  simulated historical ones — a cosmetic side effect of reusing
  log_decision() unmodified, not a correctness issue for the trades
  themselves.
- An extremely rare edge case is NOT modeled: if a position opens (via an
  EMA5 touch closing an old invalid one) and ALSO hits its own
  take-profit/stop-loss on that exact same tick, only the close is
  recorded — the same approximation-not-ground-truth spirit as the
  points above, not expected to matter at realistic tick granularity.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable

import pandas as pd

from bot.backtest.connector import BacktestConnector
from bot.config import AppConfig
from bot.execution.trade_executor import TradeExecutor
from bot.risk.position_sizing import calculate_lots
from bot.strategy.cross_detector import Direction
from bot.strategy.state_machine import EMAScalpEngine
from bot.strategy.state_machine_cross_confirmed import CrossConfirmedEngine
from bot.strategy.state_machine_cross_confirmed_adaptive_tp import CrossConfirmedAdaptiveTPEngine
from bot.strategy.state_machine_dual_cross import ClosedTrade, DualCrossEngine, OpenedTrade
from bot.strategy.state_machine_dual_cross_confirmed_entry import DualCrossConfirmedEntryEngine
from bot.strategy.state_machine_dual_cross_tight_exit import DualCrossTightExitEngine
from bot.strategy.state_machine_dual_cross_tight_exit_gap_ema5 import DualCrossTightExitGapEma5Engine
from bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm import DualCrossTightExitSwapConfirmEngine
from bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm_adx import DualCrossTightExitSwapConfirmAdxEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx import DualCrossConfirmedSwapAdxEngine
from bot.strategy.state_machine_dual_cross_confirmed_swap_adx_entrygate import DualCrossConfirmedSwapAdxEntrygateEngine
import bot.strategy.state_machine as state_machine_module
import bot.strategy.state_machine_cross_confirmed as state_machine_cross_confirmed_module
import bot.strategy.state_machine_cross_confirmed_adaptive_tp as state_machine_cross_confirmed_adaptive_tp_module
import bot.strategy.state_machine_dual_cross as state_machine_dual_cross_module
import bot.strategy.state_machine_dual_cross_confirmed_entry as state_machine_dual_cross_confirmed_entry_module
import bot.strategy.state_machine_dual_cross_tight_exit as state_machine_dual_cross_tight_exit_module
import bot.strategy.state_machine_dual_cross_tight_exit_gap_ema5 as state_machine_dual_cross_tight_exit_gap_ema5_module
import bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm as state_machine_dual_cross_tight_exit_swap_confirm_module
import bot.strategy.state_machine_dual_cross_tight_exit_swap_confirm_adx as state_machine_dual_cross_tight_exit_swap_confirm_adx_module
import bot.strategy.state_machine_dual_cross_confirmed_swap_adx as state_machine_dual_cross_confirmed_swap_adx_module
import bot.strategy.state_machine_dual_cross_confirmed_swap_adx_entrygate as state_machine_dual_cross_confirmed_swap_adx_entrygate_module
from bot.sessions import is_within_session as _real_is_within_session

STRATEGY_ENGINES = {
    "gap_threshold": EMAScalpEngine,
    "dual_cross": DualCrossEngine,
    "dual_cross_confirmed_entry": DualCrossConfirmedEntryEngine,
    "dual_cross_tight_exit": DualCrossTightExitEngine,
    # BACKTEST-ONLY — see state_machine_dual_cross_tight_exit_gap_ema5.py's
    # / state_machine_dual_cross_tight_exit_swap_confirm.py's module
    # docstrings. Deliberately NOT registered in main.py, must never be
    # launched live.
    "dual_cross_tight_exit_gap_ema5": DualCrossTightExitGapEma5Engine,
    "dual_cross_tight_exit_swap_confirm": DualCrossTightExitSwapConfirmEngine,
    "dual_cross_tight_exit_swap_confirm_adx": DualCrossTightExitSwapConfirmAdxEngine,
    "dual_cross_confirmed_swap_adx": DualCrossConfirmedSwapAdxEngine,
    "dual_cross_confirmed_swap_adx_entrygate": DualCrossConfirmedSwapAdxEntrygateEngine,
    "cross_confirmed": CrossConfirmedEngine,
    "cross_confirmed_adaptive_tp": CrossConfirmedAdaptiveTPEngine,
}

# dual_cross, dual_cross_confirmed_entry, dual_cross_tight_exit,
# dual_cross_tight_exit_gap_ema5, dual_cross_tight_exit_swap_confirm,
# dual_cross_tight_exit_swap_confirm_adx, dual_cross_confirmed_swap_adx,
# dual_cross_confirmed_swap_adx_entrygate, cross_confirmed, and
# cross_confirmed_adaptive_tp all return explicit OpenedTrade/ClosedTrade
# event lists from on_tick()/on_new_candle() instead of requiring
# before/after diffing (see their module docstrings) — anything in this
# set uses the shared event-consuming recording path below.
EVENT_BASED_VARIANTS = {
    "dual_cross", "dual_cross_confirmed_entry", "dual_cross_tight_exit",
    "dual_cross_tight_exit_gap_ema5", "dual_cross_tight_exit_swap_confirm",
    "dual_cross_tight_exit_swap_confirm_adx", "dual_cross_confirmed_swap_adx",
    "dual_cross_confirmed_swap_adx_entrygate",
    "cross_confirmed", "cross_confirmed_adaptive_tp",
}


def run_backtest(
    config: AppConfig,
    df_with_emas: pd.DataFrame,
    date_from: datetime,
    contract_size: float,
    point: float,
    starting_balance: float,
    tick_provider: Callable[[pd.Timestamp, pd.Timestamp], list[tuple[float, float]]] | None = None,
) -> list[dict]:
    """Returns closed trades as plain dicts shaped like the real trade
    ledger (bot/trade_ledger.py) — {"profit", "close_time", ...} — so the
    report can reuse bot/trade_stats.py's aggregation functions completely
    unmodified. `df_with_emas` must already have ema5/ema13/ema21 columns
    (bot.indicators.ema.compute_emas) and enough pre-date_from history for
    EMA warm-up (see scripts/backtest.py, which pads the fetch).

    `tick_provider`, if given, is called once per candle as
    `tick_provider(candle_start, candle_end)` and must return a
    chronologically-ordered list of `(bid, ask)` tuples — every real tick
    that occurred while that candle was forming. PHASE 1 below then
    replays those real ticks through the engine instead of the default
    2-point (low, high) synthetic approximation (see module docstring for
    why that approximation can miss a real, genuine entry). An empty list
    for a candle with no recorded ticks falls back to that candle's own
    close as a single synthetic tick, so a gap in tick history never
    silently skips a candle's stop-loss/take-profit checks entirely.
    Leave as `None` (default) to keep the original synthetic-tick
    behavior unchanged — this parameter changes nothing for any existing
    caller."""
    if config.strategy_variant not in STRATEGY_ENGINES:
        raise ValueError(f"Unknown strategy_variant '{config.strategy_variant}'")

    # Safety-critical, not optional: force shadow mode regardless of what
    # the source account's real config says. Without this, backtesting an
    # account whose settings.yaml has mode=live_execute (e.g. live1) would
    # make TradeExecutor attempt REAL mt5.order_send() calls against the
    # REAL account during what's supposed to be an offline replay. Shadow
    # mode is also what makes open_market_order()/close_position() pure
    # simulations in the first place (see connector.py's docstring) — the
    # whole reuse strategy depends on this.
    #
    # Also force reject_manual_trades off: _reject_manual_positions() calls
    # mt5.positions_get() directly (bypassing the connector entirely),
    # gated only by that flag, not by execution.mode — so it must be
    # forced off too, or a real position on the account being tested could
    # get logged/touched during replay.
    backtest_config = replace(
        config,
        execution=replace(config.execution, mode="shadow", reject_manual_trades=False),
    )

    connector = BacktestConnector(starting_balance)
    executor = TradeExecutor(backtest_config.execution, connector, backtest_config.symbol)
    engine = STRATEGY_ENGINES[backtest_config.strategy_variant](backtest_config, connector, executor)
    # Deliberately no reconcile_on_startup() — a backtest always starts
    # flat, and that method's get_open_position() would hit real MT5.

    sessions = backtest_config.sessions[backtest_config.strategy_variant]
    simulated_now: dict[str, datetime] = {"value": date_from}

    def _historical_is_within_session(session_windows):
        return _real_is_within_session(session_windows, now_utc=simulated_now["value"])

    # Every engine module imports is_within_session/POSITION_CLOSE_GRACE_PERIOD_SECONDS
    # independently (each bot/strategy/state_machine*.py file binds its own
    # copy of the name at import time) — patching one module's copy does
    # NOT affect another's, so all four need patching regardless of which
    # engine this particular run actually uses.
    original_is_within_session = state_machine_module.is_within_session
    original_is_within_session_dc = state_machine_dual_cross_module.is_within_session
    original_is_within_session_dcce = state_machine_dual_cross_confirmed_entry_module.is_within_session
    original_is_within_session_dcte = state_machine_dual_cross_tight_exit_module.is_within_session
    original_is_within_session_dcteg = state_machine_dual_cross_tight_exit_gap_ema5_module.is_within_session
    original_is_within_session_dctesc = state_machine_dual_cross_tight_exit_swap_confirm_module.is_within_session
    original_is_within_session_dctesca = state_machine_dual_cross_tight_exit_swap_confirm_adx_module.is_within_session
    original_is_within_session_dccsa = state_machine_dual_cross_confirmed_swap_adx_module.is_within_session
    original_is_within_session_dccsaeg = state_machine_dual_cross_confirmed_swap_adx_entrygate_module.is_within_session
    original_is_within_session_cc = state_machine_cross_confirmed_module.is_within_session
    original_is_within_session_cc_atp = state_machine_cross_confirmed_adaptive_tp_module.is_within_session
    state_machine_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_confirmed_entry_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_tight_exit_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_tight_exit_gap_ema5_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_tight_exit_swap_confirm_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_tight_exit_swap_confirm_adx_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_confirmed_swap_adx_module.is_within_session = _historical_is_within_session
    state_machine_dual_cross_confirmed_swap_adx_entrygate_module.is_within_session = _historical_is_within_session
    state_machine_cross_confirmed_module.is_within_session = _historical_is_within_session
    state_machine_cross_confirmed_adaptive_tp_module.is_within_session = _historical_is_within_session

    # POSITION_CLOSE_GRACE_PERIOD_SECONDS (see state_machine.py) exists to
    # absorb a LIVE broker's settlement lag between order_send() confirming
    # and positions_get() reflecting it — measured in real time.monotonic().
    # A backtest makes no real broker calls at all, so that lag doesn't
    # exist here; left at its live value, months of simulated trades
    # replay in a few real seconds, meaning the grace period would still
    # be "active" for the entire run and silently block every TP/stop-loss
    # detection. Zero is the semantically correct value for a backtest,
    # not a workaround.
    original_grace_period = state_machine_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dc = state_machine_dual_cross_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dcce = state_machine_dual_cross_confirmed_entry_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dcte = state_machine_dual_cross_tight_exit_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dcteg = state_machine_dual_cross_tight_exit_gap_ema5_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dctesc = state_machine_dual_cross_tight_exit_swap_confirm_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dctesca = state_machine_dual_cross_tight_exit_swap_confirm_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dccsa = state_machine_dual_cross_confirmed_swap_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_dccsaeg = state_machine_dual_cross_confirmed_swap_adx_entrygate_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_cc = state_machine_cross_confirmed_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    original_grace_period_cc_atp = state_machine_cross_confirmed_adaptive_tp_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS
    state_machine_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_confirmed_entry_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_tight_exit_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_tight_exit_gap_ema5_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_tight_exit_swap_confirm_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_tight_exit_swap_confirm_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_confirmed_swap_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_dual_cross_confirmed_swap_adx_entrygate_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_cross_confirmed_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0
    state_machine_cross_confirmed_adaptive_tp_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0

    is_event_based = backtest_config.strategy_variant in EVENT_BASED_VARIANTS
    # cross_confirmed/cross_confirmed_adaptive_tp entries have no
    # "concurrent" concept at all (single position, always False) — label
    # them distinctly rather than reusing dual_cross's
    # tick_cross/concurrent_tick_cross vocabulary, which would be
    # misleading (their entries aren't tick-triggered).
    event_entry_type = (
        backtest_config.strategy_variant
        if backtest_config.strategy_variant in ("cross_confirmed", "cross_confirmed_adaptive_tp")
        else None
    )

    trades: list[dict] = []
    current_entry: dict | None = None  # gap_threshold only: tracks the open position ourselves; OpenPosition has no lots field
    open_entries_dc: dict[Direction, dict] = {}  # event-based engines: dual_cross up to 2 concurrent, cross_confirmed always at most 1 — both keyed by direction

    def _spread_price(row) -> float:
        return float(row["spread"]) * point

    def _record_entry(position, lots: float, open_time, entry_type: str) -> None:
        nonlocal current_entry
        current_entry = {
            "direction": position.direction,
            "entry_price": position.entry_price,
            "take_profit": position.take_profit,
            "stop_loss": position.stop_loss,
            "lots": lots,
            "open_time": open_time,
            "entry_type": entry_type,
        }

    def _record_exit(exit_price: float, close_time, reason: str) -> None:
        nonlocal current_entry, connector
        assert current_entry is not None
        direction = current_entry["direction"]
        sign = 1 if direction == Direction.BUY else -1
        profit = (exit_price - current_entry["entry_price"]) * sign * current_entry["lots"] * contract_size
        connector.balance += profit
        trades.append({
            "profit": profit,
            "close_time": pd.Timestamp(close_time).isoformat(),
            "direction": direction.value,
            "volume": current_entry["lots"],
            "price": exit_price,
            "entry_price": current_entry["entry_price"],
            "open_time": pd.Timestamp(current_entry["open_time"]).isoformat(),
            "reason": reason,
            "entry_type": current_entry["entry_type"],
        })
        current_entry = None

    def _record_entry_dc(event: OpenedTrade, lots: float, open_time) -> None:
        if event_entry_type:
            entry_type = event_entry_type
        elif backtest_config.strategy_variant == "dual_cross_confirmed_entry":
            # Every entry in this variant is already a confirmed
            # candle-close cross (see state_machine_dual_cross_confirmed_entry.py's
            # module docstring) — never tick-based, and never concurrent
            # (this variant holds at most one position at a time) — so it
            # gets its own single, fixed label.
            entry_type = "confirmed_entry"
        elif event.is_fallback_entry:
            # Β§4b close-confirmed fallback (see state_machine_dual_cross.py's
            # module docstring) — the candle closed showing a genuine cross
            # that no tick-based entry caught during its own formation.
            entry_type = "concurrent_close_confirmed_fallback" if event.is_concurrent_entry else "close_confirmed_fallback"
        else:
            entry_type = "concurrent_tick_cross" if event.is_concurrent_entry else "tick_cross"
        open_entries_dc[event.direction] = {
            "entry_price": event.entry_price,
            "lots": lots,
            "open_time": open_time,
            "entry_type": entry_type,
        }

    def _record_exit_dc(event: ClosedTrade, close_time) -> None:
        # Unlike gap_threshold, DualCrossEngine computes its own exit_price
        # internally (stop_loss/take_profit use the position's own fixed
        # level; validation_failed/closed_by_concurrent_validation use the
        # triggering candle's close) and returns it directly on the event —
        # no need to re-derive it here the way _exit_price_for_reason does
        # for the other engine.
        entry = open_entries_dc.pop(event.direction)
        sign = 1 if event.direction == Direction.BUY else -1
        profit = (event.exit_price - entry["entry_price"]) * sign * entry["lots"] * contract_size
        connector.balance += profit
        trades.append({
            "profit": profit,
            "close_time": pd.Timestamp(close_time).isoformat(),
            "direction": event.direction.value,
            "volume": entry["lots"],
            "price": event.exit_price,
            "entry_price": entry["entry_price"],
            "open_time": pd.Timestamp(entry["open_time"]).isoformat(),
            "reason": event.category,
            "entry_type": entry["entry_type"],
        })

    def _closing_fill_price(direction: Direction, mid_price: float, spread_price: float) -> float:
        # Mirrors TradeExecutor.close_position's real logic: closing a BUY
        # sells (fills at bid == mid_price here), closing a SELL buys back
        # (fills at ask == mid_price + spread).
        return mid_price + spread_price if direction == Direction.SELL else mid_price

    def _exit_price_for_reason(category: str, position, reference_price: float, spread_price: float) -> float:
        # stop_loss/breakeven/take_profit exit at the position's own
        # pre-computed level (unchanged from before). ema59_reversal and
        # new_cross_confirmed have no such fixed level — they close at
        # whatever price was current when the engine decided to close,
        # i.e. the same reference price the driver just fed it (this
        # candle's close if triggered from on_new_candle, or the current
        # synthetic tick if triggered from the on_tick loop).
        if category == "stop_loss":
            return position.stop_loss
        if category == "breakeven":
            return position.entry_price
        if category == "take_profit":
            return position.take_profit
        if category in ("ema59_reversal", "new_cross_confirmed"):
            return _closing_fill_price(position.direction, reference_price, spread_price)
        raise ValueError(f"Unknown close category from engine.last_close_reason: {category!r}")

    try:
        start_idx = df_with_emas.index.searchsorted(pd.Timestamp(date_from))
        # Last usable i is len-2: on_new_candle always ignores iloc[-1] as
        # the "live/forming" candle (see cross_detector.py), so the final
        # bar in the fetched range is never itself evaluated — matches the
        # live convention exactly, not a bug.
        for i in range(start_idx, len(df_with_emas) - 1):
            candle = df_with_emas.iloc[i]
            candle_time = df_with_emas.index[i]
            simulated_now["value"] = candle_time.to_pydatetime()
            spread_price = _spread_price(candle)

            # PHASE 1: simulate this candle's own tick range WHILE IT'S
            # STILL FORMING — matches live's real order exactly (ticks
            # happen continuously as a candle forms; on_new_candle only
            # ever fires once a candle has actually closed, at which point
            # ticks are already flowing for the NEXT candle, never the one
            # that just closed). This is what lets EMA5-touch, exit checks
            # for whatever was already open, and the new early-entry check
            # all see real "not yet closed" price movement, same as live —
            # not information from this candle's own eventual close, which
            # doesn't exist yet at this point in time.
            if tick_provider is not None:
                candle_end_time = df_with_emas.index[i + 1]
                real_ticks = tick_provider(candle_time, candle_end_time)
                tick_pairs = real_ticks if real_ticks else [(float(candle["close"]), float(candle["close"]) + spread_price)]
            elif candle["close"] >= candle["open"]:
                tick_pairs = [(float(candle["low"]), float(candle["low"]) + spread_price), (float(candle["high"]), float(candle["high"]) + spread_price)]
            else:
                tick_pairs = [(float(candle["high"]), float(candle["high"]) + spread_price), (float(candle["low"]), float(candle["low"]) + spread_price)]

            for mid_price, ask_price in tick_pairs:
                connector.bid = mid_price
                connector.ask = ask_price

                if is_event_based:
                    # dual_cross / cross_confirmed return their own list of
                    # events — no before/after diffing needed (see each
                    # engine module's own docstring and OpenedTrade/ClosedTrade).
                    # cross_confirmed's on_tick() only ever produces
                    # ClosedTrade (SL/TP) — its entries fire from
                    # on_new_candle() in PHASE 2 below — but this loop stays
                    # generic over both event types for either engine.
                    for ev in engine.on_tick(connector.get_tick(backtest_config.symbol)):
                        if isinstance(ev, ClosedTrade):
                            _record_exit_dc(ev, candle_time)
                        elif isinstance(ev, OpenedTrade):
                            lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                            _record_entry_dc(ev, lots, candle_time)
                    continue

                prev_position = engine.open_position
                # Captured before on_tick() so an entry can be correctly
                # labeled: _check_early_entry() (early_entry_threshold_usd)
                # only ever fires when pending is None, while
                # _check_ema5_touch() always clears a real pending setup —
                # the only way to tell these two tick-triggered entry paths
                # apart from outside the engine, since both open a position
                # from inside this same on_tick() call.
                prev_pending = engine.pending
                engine.on_tick(connector.get_tick(backtest_config.symbol))
                new_position = engine.open_position

                if prev_position is not None and new_position is not prev_position:
                    category = engine.last_close_reason
                    exit_price = _exit_price_for_reason(category, prev_position, mid_price, spread_price)
                    _record_exit(exit_price, candle_time, reason=category)
                if new_position is not None and new_position is not prev_position:
                    lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                    entry_type = "ema5_touch" if prev_pending is not None else "early_entry"
                    _record_entry(new_position, lots, candle_time, entry_type=entry_type)

            # PHASE 2: the candle actually "closes" now — evaluate a fresh
            # cross using its own final, real close-based EMA13/21, and
            # re-check the open position's validity (which may itself
            # synchronously close-then-reopen it — see below). Use this
            # candle's own close as the reference, matching how live's
            # main loop calls on_new_candle immediately followed by
            # on_tick with a near-simultaneous real price.
            window = df_with_emas.iloc[:i + 2]
            connector.bid = float(candle["close"])
            connector.ask = connector.bid + spread_price

            if is_event_based:
                # dual_cross's on_new_candle() returns ClosedTrade events
                # (Β§4 validation) and, since 2026-08-17, can also return an
                # OpenedTrade for its close-confirmed fallback entry (Β§4b —
                # see state_machine_dual_cross.py's module docstring).
                # cross_confirmed's on_new_candle() is its ONLY entry
                # trigger, so it always returns either type — stay generic
                # over both regardless of which engine is running.
                for ev in engine.on_new_candle(window):
                    if isinstance(ev, ClosedTrade):
                        _record_exit_dc(ev, candle_time)
                    elif isinstance(ev, OpenedTrade):
                        lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                        _record_entry_dc(ev, lots, candle_time)
                continue

            prev_position = engine.open_position
            engine.on_new_candle(window)
            new_position = engine.open_position

            if prev_position is not None and new_position is not prev_position:
                category = engine.last_close_reason
                exit_price = _exit_price_for_reason(category, prev_position, float(candle["close"]), spread_price)
                _record_exit(exit_price, candle_time, reason=category)
            if new_position is not None and new_position is not prev_position:
                lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                # The ONLY way on_new_candle can now open a position
                # synchronously: _recheck_position_validity's EMA5/EMA9
                # reversal close immediately re-entering the now-confirmed
                # opposite direction (see state_machine.py). There is no
                # more close-based "immediate" gap-threshold entry at all
                # (see _decide_entry's docstring) — the EMA5-touch-wait
                # path still opens later, via on_tick, on a LATER candle's
                # own tick range (see PHASE 1 above).
                _record_entry(new_position, lots, candle_time, entry_type="ema59_reentry")
    finally:
        state_machine_module.is_within_session = original_is_within_session
        state_machine_dual_cross_module.is_within_session = original_is_within_session_dc
        state_machine_dual_cross_confirmed_entry_module.is_within_session = original_is_within_session_dcce
        state_machine_dual_cross_tight_exit_module.is_within_session = original_is_within_session_dcte
        state_machine_dual_cross_tight_exit_gap_ema5_module.is_within_session = original_is_within_session_dcteg
        state_machine_dual_cross_tight_exit_swap_confirm_module.is_within_session = original_is_within_session_dctesc
        state_machine_dual_cross_tight_exit_swap_confirm_adx_module.is_within_session = original_is_within_session_dctesca
        state_machine_dual_cross_confirmed_swap_adx_module.is_within_session = original_is_within_session_dccsa
        state_machine_dual_cross_confirmed_swap_adx_entrygate_module.is_within_session = original_is_within_session_dccsaeg
        state_machine_cross_confirmed_module.is_within_session = original_is_within_session_cc
        state_machine_cross_confirmed_adaptive_tp_module.is_within_session = original_is_within_session_cc_atp
        state_machine_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period
        state_machine_dual_cross_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dc
        state_machine_dual_cross_confirmed_entry_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dcce
        state_machine_dual_cross_tight_exit_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dcte
        state_machine_dual_cross_tight_exit_gap_ema5_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dcteg
        state_machine_dual_cross_tight_exit_swap_confirm_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dctesc
        state_machine_dual_cross_tight_exit_swap_confirm_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dctesca
        state_machine_dual_cross_confirmed_swap_adx_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dccsa
        state_machine_dual_cross_confirmed_swap_adx_entrygate_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_dccsaeg
        state_machine_cross_confirmed_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_cc
        state_machine_cross_confirmed_adaptive_tp_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period_cc_atp

    return trades
