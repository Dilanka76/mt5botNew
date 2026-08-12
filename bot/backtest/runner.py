"""Replays historical price data through the exact same, unmodified live
strategy engine (EMAScalpEngine/EMA5OnlyEngine) to produce a much larger
sample of hypothetical trades than the live/demo accounts have
accumulated so far. See docs/STRATEGY.md and the plan this was built
from for the full methodology and its known limitations — summarized
inline below at each point they matter.

This is an approximation, not ground truth:
- No real historical tick data — each 1-minute candle's own high/low are
  used as two synthetic ticks, in candle-direction order (close>=open ->
  low then high, else high then low). If a single bar's range spans both
  a profit target AND a stop-loss, that ordering determines the outcome
  (only relevant when stop_loss_usd is configured — with it off, the
  only tick-checked exit is TP, so this doesn't apply to backtesting
  either real account's current live configuration).
- Spread is synthesized from each candle's own MT5-reported spread
  (points) and the symbol's point size, not the real historical spread
  at that instant.
- decisions.jsonl gets real "now" timestamps during a backtest run, not
  simulated historical ones — a cosmetic side effect of reusing
  log_decision() unmodified, not a correctness issue for the trades
  themselves.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pandas as pd

from bot.backtest.connector import BacktestConnector
from bot.config import AppConfig
from bot.execution.trade_executor import TradeExecutor
from bot.risk.position_sizing import calculate_lots
from bot.strategy.cross_detector import Direction
from bot.strategy.state_machine import EMAScalpEngine
from bot.strategy.state_machine_ema5_only import EMA5OnlyEngine
import bot.strategy.state_machine as state_machine_module
from bot.sessions import is_within_session as _real_is_within_session

STRATEGY_ENGINES = {"gap_threshold": EMAScalpEngine, "ema5_only": EMA5OnlyEngine}


def run_backtest(
    config: AppConfig,
    df_with_emas: pd.DataFrame,
    date_from: datetime,
    contract_size: float,
    point: float,
    starting_balance: float,
) -> list[dict]:
    """Returns closed trades as plain dicts shaped like the real trade
    ledger (bot/trade_ledger.py) — {"profit", "close_time", ...} — so the
    report can reuse bot/trade_stats.py's aggregation functions completely
    unmodified. `df_with_emas` must already have ema5/ema13/ema21 columns
    (bot.indicators.ema.compute_emas) and enough pre-date_from history for
    EMA warm-up (see scripts/backtest.py, which pads the fetch)."""
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

    original_is_within_session = state_machine_module.is_within_session
    state_machine_module.is_within_session = _historical_is_within_session

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
    state_machine_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = 0.0

    trades: list[dict] = []
    current_entry: dict | None = None  # tracks the open position ourselves; OpenPosition has no lots field

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

    def _closing_fill_price(direction: Direction, mid_price: float, spread_price: float) -> float:
        # Mirrors TradeExecutor.close_position's real logic: closing a BUY
        # sells (fills at bid == mid_price here), closing a SELL buys back
        # (fills at ask == mid_price + spread).
        return mid_price + spread_price if direction == Direction.SELL else mid_price

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

            window = df_with_emas.iloc[:i + 2]

            # An immediate entry (gap < threshold) fires synchronously
            # inside on_new_candle, via open_market_order()'s own
            # connector.get_tick() call — without this, it would use
            # whatever bid/ask the PREVIOUS candle's tick loop last left
            # behind. Use this candle's own close as the reference,
            # matching how live's main loop calls on_new_candle immediately
            # followed by on_tick with a near-simultaneous real price.
            connector.bid = float(candle["close"])
            connector.ask = connector.bid + spread_price

            prev_position = engine.open_position
            engine.on_new_candle(window)
            new_position = engine.open_position

            if prev_position is not None and new_position is not prev_position:
                exit_price = _closing_fill_price(prev_position.direction, float(candle["close"]), spread_price)
                _record_exit(exit_price, candle_time, reason="opposite_cross")
            if new_position is not None and new_position is not prev_position:
                lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                # Always the gap < threshold path — on_new_candle only ever
                # opens a position synchronously for an immediate entry;
                # the EMA5-touch-wait path opens later, via on_tick below.
                _record_entry(new_position, lots, candle_time, entry_type="immediate")

            if candle["close"] >= candle["open"]:
                tick_sequence = (float(candle["low"]), float(candle["high"]))
            else:
                tick_sequence = (float(candle["high"]), float(candle["low"]))

            for mid_price in tick_sequence:
                connector.bid = mid_price
                connector.ask = mid_price + spread_price

                prev_position = engine.open_position
                engine.on_tick(connector.get_tick(backtest_config.symbol))
                new_position = engine.open_position

                if prev_position is not None and new_position is not prev_position:
                    if prev_position.stop_loss is not None and (
                        (prev_position.direction == Direction.BUY and mid_price <= prev_position.stop_loss)
                        or (prev_position.direction == Direction.SELL and mid_price >= prev_position.stop_loss)
                    ):
                        _record_exit(prev_position.stop_loss, candle_time, reason="stop_loss")
                    else:
                        _record_exit(prev_position.take_profit, candle_time, reason="take_profit")
                if new_position is not None and new_position is not prev_position:
                    lots = calculate_lots(connector.balance, backtest_config.position_sizing)
                    _record_entry(new_position, lots, candle_time, entry_type="ema5_touch")
    finally:
        state_machine_module.is_within_session = original_is_within_session
        state_machine_module.POSITION_CLOSE_GRACE_PERIOD_SECONDS = original_grace_period

    return trades
