"""Backtests several stop_loss_usd values against the SAME real historical
data and strategy engine, to compare which is actually best rather than
guessing from risk/reward math alone.

Unlike the earlier what-if analysis scripts (breakeven_stop_analysis.py,
volatility_filter_analysis.py, ...), this does NOT need any custom
re-simulation logic: stop_loss_usd (like breakeven_trigger_usd) is a real
field on AppConfig that bot/backtest/runner.py's run_backtest() already
understands natively, using the exact same live engine and exit-priority
order (stop-loss, then breakeven, then take-profit) main.py runs with. So
this script just calls run_backtest() once per candidate value, on one
shared OHLC fetch, and compares bot/trade_stats.py's own summary stats —
no separate simulation code to get subtly wrong.

breakeven_trigger_usd is taken from the account's OWN current config and
held fixed across every stop-loss value tested (not swept itself) — the
real question here is "given breakeven is already live, which stop-loss
pairs best with it," not a fresh combined sweep of both.

    python scripts/stop_loss_sweep_analysis.py --account demo1 --from 2026-05-01 --to 2026-08-13
    python scripts/stop_loss_sweep_analysis.py --account demo1 --from 2026-05-01 --to 2026-08-13 --values 15,10,5,0

Connects to MT5 only to pull historical candles + the symbol's contract
size/point/current balance, then disconnects before any replay — never
touches the live/demo trading connection or places any order.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.runner import run_backtest
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.trade_stats import compute_day_stats

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument(
        "--values", default="15,10,5",
        help="Comma-separated stop_loss_usd candidates to test, in dollars "
             "(default: 15,10,5 — the three values asked about). Use 'none' "
             "for one entry to also test with stop-loss off entirely.",
    )
    parser.add_argument(
        "--balance", type=float, default=None,
        help="Starting simulated balance for every variant. Defaults to the account's current real balance.",
    )
    return parser.parse_args()


def parse_values(raw: str) -> list[float | None]:
    out: list[float | None] = []
    for part in raw.split(","):
        part = part.strip()
        out.append(None if part.lower() == "none" else float(part))
    return out


def reason_counts(trades: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in trades:
        counts[t["reason"]] = counts.get(t["reason"], 0) + 1
    return counts


def main() -> None:
    args = parse_args()
    values = parse_values(args.values)

    config = load_config(args.account)
    setup_logging(config.logging, f"{args.account}-backtest")

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if date_to <= date_from:
        raise ValueError("--to must be after --from")

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to)
        symbol_info = connector.symbol_info(config.symbol)
        contract_size = symbol_info.trade_contract_size
        point = symbol_info.point
        starting_balance = args.balance if args.balance is not None else connector.account_info().balance
    finally:
        connector.disconnect()  # every replay below is fully offline from here

    df = compute_emas(df, config.ema_periods)

    print(f"Account: {args.account}   Range: {args.date_from} to {args.date_to} (UTC)")
    print(f"Fixed breakeven_trigger_usd (this account's current live value): {config.breakeven_trigger_usd}")
    print(f"Starting balance for every variant: {starting_balance:.2f}")
    print()

    rows = []
    for sl in values:
        variant_config = replace(config, stop_loss_usd=sl)
        trades = run_backtest(variant_config, df, date_from, contract_size, point, starting_balance)
        summary = compute_day_stats(trades)
        reasons = reason_counts(trades)
        rows.append((sl, summary, reasons))

    label_w = 14
    print(f"{'stop_loss_usd':<{label_w}}{'trades':>8}{'win%':>8}{'total P/L':>12}{'avg win':>10}{'avg loss':>10}")
    print("-" * (label_w + 48))
    for sl, summary, _ in rows:
        label = "off" if sl is None else f"{sl:g}"
        print(
            f"{label:<{label_w}}{summary['total_trades']:>8}{summary['win_rate']:>7.1f}%"
            f"{summary['total_pl']:>12.2f}{summary['avg_win']:>10.2f}{summary['avg_loss']:>10.2f}"
        )

    print()
    print("Exit-reason breakdown per variant (how each stop-loss value actually gets used):")
    for sl, _, reasons in rows:
        label = "off" if sl is None else f"{sl:g}"
        parts = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        print(f"  {label:<{label_w}}{parts}")

    best = max(rows, key=lambda r: r[1]["total_pl"])
    best_label = "off" if best[0] is None else f"{best[0]:g}"
    print()
    print(f"Best by total P/L on this data: stop_loss_usd={best_label} ({best[1]['total_pl']:+.2f})")
    print()
    print("Reminder: this is one backtest run on one historical window, not an out-of-sample test —")
    print("see the volatility-filter experiment's overfitting caveat before treating a close margin here")
    print("as a confident, permanent choice.")


if __name__ == "__main__":
    main()