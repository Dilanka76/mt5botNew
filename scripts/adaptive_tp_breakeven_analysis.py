"""Backtest-only analysis, combining two ideas the user asked to finalize
together (2026-08-12):

1. Breakeven-stop at +$3 (was +$2 in the standalone
   breakeven_stop_analysis.py experiment): once a trade moves $3 in its
   favor, a return to the entry price becomes an extra exit condition,
   alongside the existing take-profit and opposite-cross exits.
2. Adaptive take-profit: normally $5. After a real LOSS, the *next*
   trade's target becomes $6 and stays there (no further escalation)
   until a trade actually WINS (by either hitting its target or closing
   profitably via an opposite cross), which resets the target back to
   $5 for the trade after that. A breakeven trade (from rule 1) changes
   nothing — the target carries over unchanged, exactly as it was
   before that trade.

Unlike every other backtest-only script in this project so far, this
one is NOT a pure post-hoc filter over an existing trade list — raising
the target to $6 changes *whether and when* a trade exits, which can
turn an original win into something else entirely. So each trade is
independently re-simulated tick-by-tick (same synthetic-tick
methodology as scripts/backtest.py) from its own real, unchanged entry
(direction/price/open_time all come straight from an existing
scripts/backtest.py run's .trades.jsonl — only exits are touched) through
to either its own exit trigger or the *next* trade's real open_time,
which represents exactly the moment the live engine's own opposite
cross would have fired (confirmed against bot/backtest/runner.py: an
opposite-cross close and the following re-entry happen on the very same
candle). Position size is also recomputed per trade from a running
simulated balance, the same way scripts/backtest.py itself does, since
this rule can change the P/L path enough to shift which lot-size tier
applies later on.

    python scripts/adaptive_tp_breakeven_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11
    python scripts/adaptive_tp_breakeven_analysis.py --account demo1 --from 2026-02-01 --to 2026-08-11 --breakeven-trigger 3.0 --loss-tp 6.0

Read-only: connects to MT5 only to fetch historical candles and the
symbol's contract size/point, then disconnects before the (offline)
replay begins — same as every other script here. Requires
scripts/backtest.py to have already been run for this account/range.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.trade_stats import compute_day_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--breakeven-trigger", type=float, default=3.0, help="Dollars of favorable movement before the breakeven stop arms (default: 3.0)")
    parser.add_argument("--loss-tp", type=float, default=6.0, help="Take-profit target for the trade right after a real loss (default: 6.0)")
    parser.add_argument("--starting-balance", type=float, default=None, help="Defaults to the account's current real balance, same as scripts/backtest.py")
    return parser.parse_args()


def load_trades(account: str, date_from: str, date_to: str) -> list[dict]:
    path = PROJECT_ROOT / "reports" / "backtest" / account / f"{date_from}_{date_to}.trades.jsonl"
    if not path.exists():
        raise SystemExit(
            f"{path} not found — run scripts/backtest.py for this account/range first:\n"
            f"  python scripts/backtest.py --account {account} --from {date_from} --to {date_to}"
        )
    trades = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            trades.append(json.loads(line))
    return trades


def simulate_one_trade(
    trade: dict,
    window: pd.DataFrame,
    target: float,
    breakeven_trigger: float,
    stop_loss_usd: float | None,
    point: float,
) -> tuple[float, str]:
    """Returns (exit_price, exit_reason) for one trade, given the candles
    from its own open through (and including) the next trade's own open
    candle. The final candle in `window` is never tick-scanned — it only
    supplies the opposite-cross fallback close price, matching how the
    live engine's opposite-cross exit always happens at that candle's
    close, before that same candle's own tick range is ever evaluated
    for the position it just closed (see bot/backtest/runner.py)."""
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    sign = 1 if direction == "BUY" else -1
    armed = False

    if len(window) >= 2:
        for _, candle in window.iloc[:-1].iterrows():
            spread_price = float(candle["spread"]) * point
            if candle["close"] >= candle["open"]:
                tick_sequence = (float(candle["low"]), float(candle["high"]))
            else:
                tick_sequence = (float(candle["high"]), float(candle["low"]))
            for bid in tick_sequence:
                favorable = (bid - entry_price) * sign

                if stop_loss_usd is not None and favorable <= -stop_loss_usd:
                    return entry_price - sign * stop_loss_usd, "stop_loss"
                if armed and favorable <= 0:
                    return entry_price, "breakeven_stop"
                if not armed and favorable >= breakeven_trigger:
                    armed = True
                if favorable >= target:
                    return entry_price + sign * target, "take_profit"

    # Nothing triggered before the next trade's own open candle -> this
    # position would have been closed by the opposite cross that opened
    # that next trade, at that candle's own close (mirrors
    # runner.py's _closing_fill_price exactly).
    boundary_candle = window.iloc[-1]
    close = float(boundary_candle["close"])
    spread_price = float(boundary_candle["spread"]) * point
    exit_price = close + spread_price if direction == "SELL" else close
    return exit_price, "opposite_cross"


def main() -> None:
    args = parse_args()
    trades = load_trades(args.account, args.date_from, args.date_to)
    print(f"Loaded {len(trades)} trades from the existing {args.account} backtest ({args.date_from} to {args.date_to}).")
    print(f"Testing: breakeven stop at +${args.breakeven_trigger:.2f}, take-profit ${args.loss_tp:.2f} "
          f"after a loss (resets to normal after the next win).\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, date_from_dt, date_to_dt)
        symbol_info = connector.symbol_info(config.symbol)
        contract_size = symbol_info.trade_contract_size
        point = symbol_info.point
        starting_balance = args.starting_balance if args.starting_balance is not None else connector.account_info().balance
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    open_times = [pd.Timestamp(t["open_time"]) for t in trades]
    normal_tp = config.take_profit_usd
    current_tp = normal_tp
    balance = starting_balance
    stop_loss_usd = config.stop_loss_usd

    new_trades = []
    elevated_count = 0
    elevated_hit_6 = 0

    for i, t in enumerate(trades):
        open_time = open_times[i]
        boundary_time = open_times[i + 1] if i + 1 < len(trades) else df.index[-1]
        window = df.loc[open_time:boundary_time]
        if len(window) == 0:
            window = df.iloc[df.index.searchsorted(open_time):df.index.searchsorted(open_time) + 1]

        target = current_tp
        if target != normal_tp:
            elevated_count += 1

        lots = calculate_lots(balance, config.position_sizing)
        exit_price, exit_reason = simulate_one_trade(t, window, target, args.breakeven_trigger, stop_loss_usd, point)

        direction = t["direction"]
        entry_price = t["entry_price"]
        sign = 1 if direction == "BUY" else -1
        profit = (exit_price - entry_price) * sign * lots * contract_size
        balance += profit

        if exit_reason == "take_profit" and target != normal_tp:
            elevated_hit_6 += 1

        new_trades.append({
            "direction": direction,
            "entry_price": entry_price,
            "price": exit_price,
            "profit": profit,
            "reason": exit_reason,
            "volume": lots,
            "open_time": t["open_time"],
            "close_time": t["close_time"],
        })

        if profit > 1e-9:
            current_tp = normal_tp
        elif profit < -1e-9:
            current_tp = args.loss_tp
        # breakeven (profit == 0) -> current_tp carries over unchanged

    old_summary = compute_day_stats(trades)
    new_summary = compute_day_stats(new_trades)

    print(f"{'':22}{'BEFORE':>14}{'AFTER':>14}")
    print(f"{'Total trades':22}{old_summary['total_trades']:>14}{new_summary['total_trades']:>14}")
    print(f"{'Wins':22}{old_summary['wins']:>14}{new_summary['wins']:>14}")
    print(f"{'Losses':22}{old_summary['losses']:>14}{new_summary['losses']:>14}")
    print(f"{'Breakeven (scratch)':22}{old_summary['breakeven']:>14}{new_summary['breakeven']:>14}")
    print(f"{'Win rate':22}{old_summary['win_rate']:>13.1f}%{new_summary['win_rate']:>13.1f}%")
    print(f"{'Total P/L':22}{old_summary['total_pl']:>14.2f}{new_summary['total_pl']:>14.2f}")
    print(f"{'Avg win':22}{old_summary['avg_win']:>14.2f}{new_summary['avg_win']:>14.2f}")
    print(f"{'Avg loss':22}{old_summary['avg_loss']:>14.2f}{new_summary['avg_loss']:>14.2f}")
    print(f"{'Ending balance (sim)':22}{'':>14}{balance:>14.2f}   (started at {starting_balance:.2f})")

    print(f"\n{elevated_count} trades were played with the elevated ${args.loss_tp:.2f} target "
          f"(following a loss); {elevated_hit_6} of those actually reached it.")
    print("\nMethodology note: each trade is independently re-simulated tick-by-tick from its own real")
    print("entry through to the next real trade's open time (candle high/low as two synthetic ticks,")
    print("same approximation as scripts/backtest.py) — not a simple filter over the original trade list.")


if __name__ == "__main__":
    main()
