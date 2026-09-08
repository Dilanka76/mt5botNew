"""Fit stop / take-profit / runner for a new timeframe leg, walk-forward.

User's question 2026-09-08, after M5 passed the naked-cross screen:
*"how need to finalize the m5, i am asking the strategy how need to be
finalize"*

The RULES do not change -- same EMA13/21 confirmed cross, same swap on
the opposite cross, same breakeven, same TP-runner. Only the dollar
levels change, because they were fitted to M3 candles. This fits them
from data instead of scaling them by hand.

METHOD -- walk-forward, not "best over the whole window":
  Every candidate is replayed through the REAL engine (bot.backtest.runner,
  the same code path the live bot uses). Trades are then split by date:
  the winner is chosen on the FIRST half alone and its SECOND-half result
  is reported beside it. A combination that tops the first half and
  collapses in the second is a curve fit, and the table makes that
  visible rather than hiding it behind one pooled number.

  Also reported: how many of the first half's top five stay positive in
  the second. If the good scores are scattered rather than clustered,
  the grid is fitting noise and none of it should be deployed.

TWO THINGS ARE DELIBERATELY NOT SWEPT:

  breakeven_trigger_usd is DERIVED, always equal to the runner's arm
  point (take_profit - arm_before). On 2026-09-08 M3 was left with
  breakeven at $5.50 while the take-profit was removed at $5.00, so a
  +$5 winner could still run to -$7. Deriving it makes that gap
  impossible to reopen.

  tp_runner_arm_before_usd stays FIXED at M3's $1.00 and is not scaled
  with the candle. It exists to win a race against the broker's
  take-profit limit order, which fills in microseconds while the bot
  polls once a second. That race is decided by how far price travels per
  SECOND, which is identical on every chart -- M5 candles are bigger,
  but the market underneath them is the same market. Scaling this value
  by candle size would be a category error.

    python scripts/fit_new_timeframe.py --account demo1_m3 --timeframe M5 \
        --from 2026-03-10 --to 2026-09-08

Read-only: fetches candles once, then replays entirely offline.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.backtest.runner import run_backtest
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.timeframes import TIMEFRAME_MINUTES
from bot.trade_stats import compute_day_stats


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, type=validate_account_name,
                   help="config to borrow rules/credentials from; its timeframe is overridden")
    p.add_argument("--timeframe", required=True, help="the timeframe being fitted, e.g. M5")
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--stops", default="6,7,8,9,10,11,12")
    p.add_argument("--tps", default="6,7,8,9,10,11")
    p.add_argument("--arm-before", type=float, default=1.0,
                   help="NOT scaled with the candle — see the module docstring")
    p.add_argument("--locks", default="0,1,2", help="tp_runner_lock_below_usd candidates")
    p.add_argument("--trails", default="0.5,0.75,1.0,1.5")
    p.add_argument("--balance", type=float, default=100000.0,
                   help="same starting balance for every candidate, so position sizing "
                        "cannot masquerade as edge (it did in the 2026-09-06 stop sweep)")
    return p.parse_args()


def split_halves(trades: list[dict], boundary: datetime) -> tuple[list[dict], list[dict]]:
    first = [t for t in trades if t["close_time"] < boundary]
    second = [t for t in trades if t["close_time"] >= boundary]
    return first, second


def evaluate(config, df, date_from, boundary, contract_size, point, balance) -> dict:
    trades = run_backtest(config, df, date_from, contract_size, point, balance)
    first, second = split_halves(trades, boundary)
    return {
        "n": len(trades),
        "first": compute_day_stats(first)["total_pl"] if first else 0.0,
        "second": compute_day_stats(second)["total_pl"] if second else 0.0,
        "n1": len(first), "n2": len(second),
        "stats": compute_day_stats(trades),
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    setup_logging(config.logging, f"{args.account}-fit-{args.timeframe}")

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if date_to <= date_from:
        raise ValueError("--to must be after --from")
    boundary = date_from + (date_to - date_from) / 2

    timeframe = args.timeframe.upper()
    minutes = TIMEFRAME_MINUTES[timeframe]
    warmup = date_from - timedelta(minutes=config.candles_to_fetch * minutes)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, timeframe, warmup, date_to)
        info = connector.symbol_info(config.symbol)
        contract_size, point = info.trade_contract_size, info.point
    finally:
        connector.disconnect()
    df = compute_emas(df, config.ema_periods)

    base = replace(config, timeframe=timeframe, swap_immediate=True,
                   tp_runner_trail_usd=None)          # stage 1 runs with the runner OFF

    print("=" * 96)
    print(f"FITTING {timeframe} — rules borrowed from {args.account}, levels fitted from data")
    print(f"range {args.date_from} .. {args.date_to} (UTC), split at {boundary:%Y-%m-%d}")
    print(f"{len(df)} candles, balance ${args.balance:,.0f} for every candidate")
    print(f"sessions inherited from {args.account} — session choice is a SEPARATE decision")
    print("=" * 96)
    print("Winner is chosen on the FIRST half only. The second-half column is the test,")
    print("not part of the selection.\n")

    # ---- stage 1: stop x take-profit, runner off ----------------------
    stops = [float(v) for v in args.stops.split(",")]
    tps = [float(v) for v in args.tps.split(",")]
    print(f"STAGE 1 — stop x take-profit ({len(stops) * len(tps)} combinations, runner off)")
    print(f"  {'stop':>6}{'TP':>6}{'trades':>8}{'1st half':>11}{'2nd half':>11}{'total':>11}{'win%':>8}")
    rows = []
    for sl in stops:
        for tp in tps:
            cfg = replace(base, stop_loss_usd=sl, take_profit_usd=tp,
                          breakeven_trigger_usd=max(0.5, tp - args.arm_before))
            r = evaluate(cfg, df, date_from, boundary, contract_size, point, args.balance)
            r.update(stop=sl, tp=tp)
            rows.append(r)
            print(f"  {sl:>6.1f}{tp:>6.1f}{r['n']:>8}{r['first']:>11.0f}{r['second']:>11.0f}"
                  f"{r['stats']['total_pl']:>11.0f}{r['stats']['win_rate']:>7.1f}%")

    ranked = sorted(rows, key=lambda r: r["first"], reverse=True)
    best = ranked[0]
    survivors = sum(1 for r in ranked[:5] if r["second"] > 0)
    print(f"\n  Chosen on first half : stop ${best['stop']:.1f} / TP ${best['tp']:.1f}"
          f"  (1st ${best['first']:+,.0f})")
    print(f"  Its second half      : ${best['second']:+,.0f}"
          f"   <- the honest number")
    print(f"  Top-5 stability      : {survivors} of the first half's best 5 are also positive "
          f"in the second half")
    if survivors <= 2:
        print("  WARNING: the good scores do not cluster. That is what fitting noise looks")
        print("           like, and none of this grid should be deployed on it.")

    # ---- stage 2: the runner, on stage 1's winner ---------------------
    tp = best["tp"]
    print(f"\nSTAGE 2 — TP-runner on stop ${best['stop']:.1f} / TP ${tp:.1f}, "
          f"arm ${args.arm_before:.2f} early (fixed, not scaled)")
    print(f"  {'lock at':>9}{'trail':>8}{'trades':>8}{'1st half':>11}{'2nd half':>11}{'total':>11}")
    off = next(r for r in rows if r["stop"] == best["stop"] and r["tp"] == tp)
    print(f"  {'runner off':>9}{'-':>8}{off['n']:>8}{off['first']:>11.0f}{off['second']:>11.0f}"
          f"{off['stats']['total_pl']:>11.0f}")

    runner_rows = []
    for lock in [float(v) for v in args.locks.split(",")]:
        if lock >= tp:
            continue
        for trail in [float(v) for v in args.trails.split(",")]:
            cfg = replace(base, stop_loss_usd=best["stop"], take_profit_usd=tp,
                          breakeven_trigger_usd=max(0.5, tp - args.arm_before),
                          tp_runner_trail_usd=trail,
                          tp_runner_arm_before_usd=args.arm_before,
                          tp_runner_lock_below_usd=lock)
            r = evaluate(cfg, df, date_from, boundary, contract_size, point, args.balance)
            r.update(lock=lock, trail=trail)
            runner_rows.append(r)
            print(f"  {tp - lock:>9.2f}{trail:>8.2f}{r['n']:>8}{r['first']:>11.0f}"
                  f"{r['second']:>11.0f}{r['stats']['total_pl']:>11.0f}")

    if runner_rows:
        rbest = max(runner_rows, key=lambda r: r["first"])
        gain1 = rbest["first"] - off["first"]
        gain2 = rbest["second"] - off["second"]
        print(f"\n  Best runner on first half: lock +${tp - rbest['lock']:.2f}, "
              f"trail ${rbest['trail']:.2f}")
        print(f"    vs runner off — first half ${gain1:+,.0f}, second half ${gain2:+,.0f}")
        if gain2 <= 0:
            print("    The runner does NOT carry into the second half here. Ship the leg")
            print("    with the runner OFF and revisit it separately.")

    print(f"\n{'=' * 96}")
    print("FINAL SPEC to write into the new config (only if stage 1 stability was 4 or 5):")
    print(f"  timeframe                : {timeframe}")
    print(f"  stop_loss_usd            : {best['stop']:.2f}")
    print(f"  take_profit_usd          : {tp:.2f}")
    print(f"  breakeven_trigger_usd    : {max(0.5, tp - args.arm_before):.2f}   (= the arm point, derived)")
    print(f"  tp_runner_arm_before_usd : {args.arm_before:.2f}   (fixed — a race against the broker, not a candle size)")
    if runner_rows:
        print(f"  tp_runner_lock_below_usd : {rbest['lock']:.2f}")
        print(f"  tp_runner_trail_usd      : {rbest['trail']:.2f}")
    print(f"  swap_immediate           : true")
    print("\nStill decided OUTSIDE this script: lot size (set it so stop x lots matches the")
    print("risk per trade you already accept), sessions, and the daily loss limit.")


if __name__ == "__main__":
    main()
