"""Backtest-only analysis: an HONEST (non-lookahead) version of "enter
when EMA13/EMA21 are close enough, inside the cross candle" — sweeps a
few small dollar thresholds and shows the real, trustworthy result for
each, unlike scripts/equal_point_interpolation_analysis.py's earlier
version (which used the cross candle's own final, already-closed EMA
values — a lookahead bug, since those values don't exist yet at the
moment a live bot would need to act on them).

THE HONEST METHOD:
For each real immediate-entry trade, take the previous (already fully
closed, real) candle's EMA13/EMA21 — this part is legitimate, no
lookahead. Then walk the cross candle's own two synthetic ticks (its
low and high, in candle-direction order — same convention
bot/backtest/runner.py already uses), and at EACH tick, recompute what
EMA13/EMA21 WOULD BE if the candle were closing right now, at that
tick's price:

    provisional_ema = tick_price * k + prev_real_ema * (1 - k),  k = 2/(period+1)

This only ever uses information that would genuinely be available at
that exact moment — the previous candle's real close, and the current
tick's real price. No cheating with the candle's actual eventual close.

At each tick, check |provisional_ema13 - provisional_ema9| ... wait —
check |provisional_ema13 - provisional_ema21| <= threshold. The first
tick (in order) where that's true is the entry point for that
threshold; if neither tick satisfies it, the trade falls back to its
real, unmodified entry (this idea simply doesn't change anything for
that trade at that threshold).

Deliberately constrained to inside the cross candle only (not any
earlier candle) — letting the threshold check run continuously across
all candles would be a much bigger, different, harder-to-reason-about
change; see the conversation this was built from for why that's a
deliberate scope decision, not an oversight.

Same first-order-estimate caveat as equal_point_interpolation_analysis.py:
keeps each trade's real exit price/reason unchanged, only substitutes
entry — not a full re-simulation.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import Direction
from bot.trade_stats import compute_day_stats

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument(
        "--thresholds", default="0.05,0.10,0.25,0.50",
        help="Comma-separated dollar thresholds to sweep (default: 0.05,0.10,0.25,0.50)",
    )
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


def provisional_ema(tick_price: float, prev_real_ema: float, period: int) -> float:
    k = 2 / (period + 1)
    return tick_price * k + prev_real_ema * (1 - k)


def find_honest_entry(
    prev_ema13: float, prev_ema21: float, mid_period: int, slow_period: int,
    cross_open: float, cross_high: float, cross_low: float, cross_close: float,
    threshold: float,
) -> float | None:
    """Returns the tick price where the honest, non-lookahead provisional
    EMA13/EMA21 gap first closes to within `threshold`, walking the cross
    candle's own two synthetic ticks in candle-direction order — or None
    if neither tick satisfies it (the trade keeps its real entry then)."""
    if cross_close >= cross_open:
        tick_sequence = (cross_low, cross_high)
    else:
        tick_sequence = (cross_high, cross_low)

    for tick_price in tick_sequence:
        p13 = provisional_ema(tick_price, prev_ema13, mid_period)
        p21 = provisional_ema(tick_price, prev_ema21, slow_period)
        if abs(p13 - p21) <= threshold:
            return tick_price
    return None


def main() -> None:
    args = parse_args()
    thresholds = [float(x) for x in args.thresholds.split(",")]
    trades = load_trades(args.account, args.date_from, args.date_to)
    immediate = [t for t in trades if t.get("entry_type") == "immediate"]
    print(f"Loaded {len(trades)} trades ({len(immediate)} immediate-entry — the only ones this idea applies to).\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from_dt - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to_dt)
        contract_size = connector.symbol_info(config.symbol).trade_contract_size
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    df = compute_emas(df, config.ema_periods)
    mid_period, slow_period = config.ema_periods.mid, config.ema_periods.slow

    # Pre-locate each trade's candle rows once, reused across every threshold.
    located = []
    unscored = 0
    for t in immediate:
        cross_time = pd.Timestamp(t["open_time"])
        pos = df.index.searchsorted(cross_time)
        if pos <= 0 or pos >= len(df):
            unscored += 1
            continue
        located.append((t, df.iloc[pos - 1], df.iloc[pos]))

    print(f"{len(located)} of {len(immediate)} immediate trades matched to real candle data ({unscored} skipped).\n")

    baseline_summary = compute_day_stats(immediate)
    print(f"{'Threshold':<12}{'Early entries':>15}{'Trades':>9}{'Win%':>8}{'Total P/L':>12}{'Avg win':>10}{'Avg loss':>10}")
    print("-" * 76)
    print(f"{'(none)':<12}{'':>15}{baseline_summary['total_trades']:>9}{baseline_summary['win_rate']:>7.1f}%"
          f"{baseline_summary['total_pl']:>12.2f}{baseline_summary['avg_win']:>10.2f}{baseline_summary['avg_loss']:>10.2f}")

    for threshold in thresholds:
        scored = []
        early_count = 0
        for t, prev_row, cross_row in located:
            entry_price = find_honest_entry(
                float(prev_row["ema13"]), float(prev_row["ema21"]), mid_period, slow_period,
                float(cross_row["open"]), float(cross_row["high"]), float(cross_row["low"]), float(cross_row["close"]),
                threshold,
            )
            direction = Direction(t["direction"])
            sign = 1 if direction == Direction.BUY else -1
            t2 = dict(t)
            if entry_price is not None:
                early_count += 1
                exit_price = t["price"]
                t2["profit"] = (exit_price - entry_price) * sign * t["volume"] * contract_size
                t2["entry_price"] = entry_price
            scored.append(t2)

        summary = compute_day_stats(scored)
        print(f"${threshold:<11.2f}{early_count:>15}{summary['total_trades']:>9}{summary['win_rate']:>7.1f}%"
              f"{summary['total_pl']:>12.2f}{summary['avg_win']:>10.2f}{summary['avg_loss']:>10.2f}")

    print()
    print("'Early entries' = how many of the immediate trades actually found a qualifying tick within the")
    print("cross candle at that threshold; the rest keep their real, unmodified entry (no change for them).")
    print("Methodology reminder: same exit price/reason kept as the real trade — a first-order estimate,")
    print("not a full re-simulation of whether a shifted take-profit would hit sooner/later/not at all.")
    print("This IS the honest, non-lookahead version — only uses each tick's own real price and the")
    print("previous (already closed) candle's real EMA values, nothing from the cross candle's own future.")


if __name__ == "__main__":
    main()
