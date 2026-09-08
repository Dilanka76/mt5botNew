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
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.backtest.runner import run_backtest
from bot.indicators.adx import compute_adx
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
    p.add_argument("--stops", default="7,8,9,10,11,12")
    p.add_argument("--tps", default="6,7,8,9,10,11")
    p.add_argument("--quick", action="store_true",
                   help="coarse 3x3 grid first: same method, ~9 replays, to see the "
                        "shape and time one run before committing to the full sweep")
    p.add_argument("--arm-before", type=float, default=1.0,
                   help="NOT scaled with the candle — see the module docstring")
    p.add_argument("--locks", default="0,1,2", help="tp_runner_lock_below_usd candidates")
    p.add_argument("--trails", default="0.5,0.75,1.0,1.5")
    p.add_argument("--jobs", type=int, default=None,
                   help="parallel worker processes (default: all cores, max 8). "
                        "--jobs 1 forces the plain serial path if anything looks wrong.")
    p.add_argument("--balance", type=float, default=100000.0,
                   help="same starting balance for every candidate, so position sizing "
                        "cannot masquerade as edge (it did in the 2026-09-06 stop sweep)")
    return p.parse_args()


def closed_at(trade: dict) -> datetime:
    """bot/backtest/runner.py records close_time as an ISO string, not a
    datetime -- it is shaped to match the real trade ledger. Parse it, and
    treat a naive timestamp as UTC so the split cannot silently compare
    across timezones."""
    value = trade["close_time"]
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def split_halves(trades: list[dict], boundary: datetime) -> tuple[list[dict], list[dict]]:
    first = [t for t in trades if closed_at(t) < boundary]
    second = [t for t in trades if closed_at(t) >= boundary]
    return first, second


# Each replay is completely independent of every other -- MT5 is already
# disconnected by the time the sweep starts, so this is pure CPU work.
# Windows spawns fresh interpreters, so the candles are shipped once per
# worker through the initializer rather than once per task.
_CTX: dict = {}


def _init_worker(ctx: dict) -> None:
    logging.getLogger("bot").setLevel(logging.WARNING)
    _CTX.update(ctx)


def _run_one(task: tuple[dict, object]) -> dict:
    labels, config = task
    r = evaluate(config, _CTX["df"], _CTX["date_from"], _CTX["boundary"],
                 _CTX["contract_size"], _CTX["point"], _CTX["balance"])
    r.update(labels)
    return r


def sweep(tasks: list[tuple[dict, object]], jobs: int, ctx: dict, label: str) -> list[dict]:
    """Run every candidate, printing progress as each finishes. Falls back
    to the serial path on --jobs 1, which stays the reference behaviour."""
    total = len(tasks)
    started = time.monotonic()
    results: list[dict] = []

    if jobs <= 1:
        _init_worker(ctx)
        for task in tasks:
            results.append(_run_one(task))
            _progress(len(results), total, started, label)
        return results

    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker,
                             initargs=(ctx,)) as pool:
        for r in pool.map(_run_one, tasks):
            results.append(r)
            _progress(len(results), total, started, label)
    return results


def _progress(done: int, total: int, started: float, label: str) -> None:
    elapsed = time.monotonic() - started
    if done == 1 or done % 5 == 0 or done == total:
        rate = elapsed / done
        left = rate * (total - done)
        print(f"    {label}: {done}/{total} done, ~{left / 60:.0f} min left", flush=True)


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
    if config.swap_adx_filter is not None:
        # Same parity rule as main.py and scripts/backtest.py: every column
        # an engine can read must exist here too.
        df = compute_adx(df, period=config.swap_adx_filter.adx_period)

    # 54 replays would otherwise log every simulated order -- tens of
    # thousands of lines that bury the table this script exists to print.
    logging.getLogger("bot").setLevel(logging.WARNING)

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
    if args.quick:
        stops, tps = [8.0, 10.0, 12.0], [7.0, 9.0, 11.0]
        print("QUICK MODE — coarse grid, for shape and timing only. The pass/fail")
        print("verdict below is NOT final; re-run without --quick before deciding.\n")
    jobs = args.jobs or min(os.cpu_count() or 1, 8)
    ctx = dict(df=df, date_from=date_from, boundary=boundary,
               contract_size=contract_size, point=point, balance=args.balance)

    tasks = [({"stop": sl, "tp": tp},
              replace(base, stop_loss_usd=sl, take_profit_usd=tp,
                      breakeven_trigger_usd=max(0.5, tp - args.arm_before)))
             for sl in stops for tp in tps]
    print(f"STAGE 1 — stop x take-profit ({len(tasks)} combinations, runner off, "
          f"{jobs} parallel)", flush=True)
    rows = sweep(tasks, jobs, ctx, "stage 1")

    print(f"\n  {'stop':>6}{'TP':>6}{'trades':>8}{'1st half':>11}{'2nd half':>11}"
          f"{'total':>11}{'win%':>8}")
    for r in sorted(rows, key=lambda r: (r["stop"], r["tp"])):
        print(f"  {r['stop']:>6.1f}{r['tp']:>6.1f}{r['n']:>8}"
              f"{r['first']:>11.0f}{r['second']:>11.0f}"
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

    runner_tasks = [
        ({"lock": lock, "trail": trail},
         replace(base, stop_loss_usd=best["stop"], take_profit_usd=tp,
                 breakeven_trigger_usd=max(0.5, tp - args.arm_before),
                 tp_runner_trail_usd=trail,
                 tp_runner_arm_before_usd=args.arm_before,
                 tp_runner_lock_below_usd=lock))
        for lock in [float(v) for v in args.locks.split(",")] if lock < tp
        for trail in [float(v) for v in args.trails.split(",")]
    ]
    runner_rows = sweep(runner_tasks, jobs, ctx, "stage 2") if runner_tasks else []
    for r in sorted(runner_rows, key=lambda r: (r["lock"], r["trail"])):
        print(f"  {tp - r['lock']:>9.2f}{r['trail']:>8.2f}{r['n']:>8}{r['first']:>11.0f}"
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
