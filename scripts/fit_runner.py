"""Fit the TP-runner from HISTORY instead of waiting for real trades.

User, 2026-09-09: *"the 5m we need the runner, so can you tell me how can
we finalized it"*.

THE PROBLEM. scripts/simulate_tp_runner.py measures the runner on real
take-profit exits read from the trade ledger. M3 had 59 of them; demo1_m5
has FOUR. And bot/backtest/runner.py does not simulate tp_runner at all
(see project_backtest_ignores_tp_runner), so the ordinary backtest cannot
answer it either. Waiting for demo1_m5 to accumulate 30 winners is a
month.

WHAT THIS DOES INSTEAD. The runner only ever acts on a trade that REACHES
its target, and those can be found in history rather than looked up:

  1. Replay every confirmed EMA13/21 cross over the window.
  2. Keep the ones that reached +take_profit before their stop or the
     opposite cross -- these are the trades a runner would have acted on.
  3. From the exact candle each one reached the target, replay the runner
     forward with scripts/simulate_tp_runner.simulate -- the same tested
     replay the real-trade study uses.
  4. Score each variant against the flat exit at the target, which is the
     only benchmark that matters: it is what the account does today.

Several hundred samples instead of four.

PRE-REGISTERED, written before the first run:
  1. Positive in the FIRST half and the SECOND half separately.
  2. Wins under BOTH candle orderings (or on --real-ticks, which removes
     the ordering question). stop-first assumes the adverse extreme came
     first and is pessimistic; ratchet-first assumes the favourable one
     did and is optimistic. A setting that only wins under one is winning
     on an artefact of candle resolution.
  3. Sits on a PLATEAU -- neighbouring trail and lock values must also
     work. A lone peak is a curve fit.
  4. Beats the flat target by enough to be worth the machinery.

Anything failing one of these is a no, however good the headline looks --
and "no runner" is a perfectly good answer. On M3 the live runner is
currently about $70 BEHIND a control that has none.

arm_before is not fitted. It exists to beat the broker's take-profit fill,
which is decided by dollars travelled per SECOND -- the same market on
every chart -- so it stays at $1.00. See create_m5_leg.py.

    python scripts/fit_runner.py --account demo1_m5 --from 2026-03-10 --to 2026-09-08
    python scripts/fit_runner.py --account demo1_m3 --from 2026-03-10 --to 2026-09-08

Read-only: fetches candles, places nothing.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from simulate_tp_runner import simulate

USD_PER_LOT_PER_DOLLAR = 100.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, type=validate_account_name)
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--locks", default="0,0.5,1,1.5,2",
                   help="tp_runner_lock_below_usd candidates (dollars BELOW the target)")
    p.add_argument("--trails", default="0.25,0.5,0.75,1,1.5,2")
    p.add_argument("--max-candles", type=int, default=300,
                   help="how far past the target to follow a trade before giving up")
    p.add_argument("--lots", type=float, default=None,
                   help="lot size for the $ columns (default: the account's top tier)")
    return p.parse_args()


def winners(df: pd.DataFrame, tp: float, stop: float | None) -> list[dict]:
    """Every cross trade that REACHED its target, and the candle it did so.

    Uses the pessimistic within-candle ordering -- the adverse extreme is
    assumed to come first, so a candle that spans both the stop and the
    target counts as stopped, not won. That undercounts winners rather
    than inventing them.
    """
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False           # row 0 has no predecessor
    crosses = list(df.index[changed])

    out = []
    for i in range(len(crosses) - 1):
        entry_t, next_cross = crosses[i], crosses[i + 1]
        is_buy = bool(above.loc[entry_t])
        entry = float(df.loc[entry_t, "close"])
        window = df.loc[entry_t:next_cross].iloc[1:]
        for idx, row in window.iterrows():
            adverse = (entry - float(row["low"])) if is_buy else (float(row["high"]) - entry)
            if stop is not None and adverse >= stop:
                break                 # stopped before it ever got there
            favorable = (float(row["high"]) - entry) if is_buy else (entry - float(row["low"]))
            if favorable >= tp:
                out.append({"entry_time": entry_t, "reached_at": idx,
                            "direction": "BUY" if is_buy else "SELL", "entry": entry})
                break
    return out


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    tp = config.take_profit_usd
    lots = args.lots if args.lots is not None else float(config.position_sizing[-1].lots)
    to_usd = lots * USD_PER_LOT_PER_DOLLAR

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe,
                            date_from - timedelta(days=2), date_to)
    finally:
        connector.disconnect()
    df = compute_emas(df, config.ema_periods)
    df = df[df.index >= date_from]

    won = winners(df, tp, config.stop_loss_usd)
    print("=" * 92)
    print(f"FIT THE RUNNER — {args.account} ({config.timeframe}) target ${tp:.2f}, "
          f"stop {'none' if config.stop_loss_usd is None else f'${config.stop_loss_usd:.2f}'}")
    print(f"{args.date_from}..{args.date_to}, {len(df)} candles, {lots} lots")
    print("=" * 92)
    print(f"{len(won)} trades reached the target — these are the ones a runner would act on.")
    if len(won) < 30:
        print("FEWER THAN 30. Too few to fit anything; widen the window before trusting this.")
    if not won:
        return
    mid = len(won) // 2
    baseline = tp * len(won)
    print(f"Baseline (take ${tp:.2f} and close): ${baseline * to_usd:,.0f} "
          f"over {len(won)} trades\n")

    print(f"  {'lock at':>8}{'trail':>7}{'ordering':>15}{'total':>11}{'vs flat':>11}"
          f"{'1st half':>11}{'2nd half':>11}   {'ran':>4}")
    print("-" * 92)
    rows = []
    for lock_below in [float(v) for v in args.locks.split(",")]:
        lock = tp - lock_below
        if lock <= 0:
            continue
        for trail in [float(v) for v in args.trails.split(",")]:
            for ordering in ("stop-first", "ratchet-first"):
                got, ran, unresolved = [], 0, 0
                for w in won:
                    r = simulate(df, w["reached_at"], w["direction"], w["entry"],
                                 lock, trail, args.max_candles, None,
                                 ratchet_first=(ordering == "ratchet-first"))
                    if r is None:
                        unresolved += 1
                        continue
                    how, profit = r
                    got.append(profit)
                    if how == "trailed out" or profit > tp:
                        ran += 1
                if not got:
                    continue
                total = sum(got)
                # Compare like with like: the baseline covers only the
                # trades that actually resolved.
                base = tp * len(got)
                first, second = got[:mid], got[mid:]
                d1 = sum(first) - tp * len(first)
                d2 = sum(second) - tp * len(second)
                rows.append({"lock": lock, "trail": trail, "ordering": ordering,
                             "delta": total - base, "d1": d1, "d2": d2,
                             "ran": ran, "n": len(got)})
                print(f"  {lock:>8.2f}{trail:>7.2f}{ordering:>15}"
                      f"{total * to_usd:>11,.0f}{(total - base) * to_usd:>+11,.0f}"
                      f"{d1 * to_usd:>+11,.0f}{d2 * to_usd:>+11,.0f}   {ran:>4}")

    # ---- verdict against the pre-registered rules ----------------------
    print("\n" + "=" * 92)
    print("VERDICT — a setting must pass ALL of these")
    print("=" * 92)
    pairs: dict[tuple[float, float], dict] = {}
    for r in rows:
        pairs.setdefault((r["lock"], r["trail"]), {})[r["ordering"]] = r
    passing = []
    for (lock, trail), both in sorted(pairs.items()):
        a, b = both.get("stop-first"), both.get("ratchet-first")
        if not (a and b):
            continue
        ok = (a["delta"] > 0 and b["delta"] > 0
              and a["d1"] > 0 and a["d2"] > 0 and b["d1"] > 0 and b["d2"] > 0)
        if ok:
            passing.append(((lock, trail), min(a["delta"], b["delta"])))
    if not passing:
        print("  NOTHING PASSES. No lock/trail combination beats simply taking the target")
        print("  in both halves under both orderings. Ship this leg with the runner OFF —")
        print("  that is a real result, not a failure to find one.")
        return
    passing.sort(key=lambda x: x[1], reverse=True)
    print(f"  {len(passing)} of {len(pairs)} combinations pass in both halves AND both orderings.")
    print(f"  Ranked by their WORST case (the pessimistic ordering), best first:\n")
    for (lock, trail), worst in passing[:8]:
        print(f"    lock +${lock:.2f}, trail ${trail:.2f}   worst case ${worst * to_usd:+,.0f}")
    (best_lock, best_trail), _ = passing[0]
    neighbours = sum(1 for (l, t), _ in passing
                     if abs(l - best_lock) <= 0.51 and abs(t - best_trail) <= 0.51
                     and (l, t) != (best_lock, best_trail))
    print(f"\n  Best: lock +${best_lock:.2f}, trail ${best_trail:.2f}")
    print(f"  Neighbours that also pass: {neighbours}")
    if neighbours < 2:
        print("  WARNING: it is a lone peak, not a plateau. That is what a curve fit looks")
        print("           like — do not deploy on it.")
    else:
        print("  It sits on a plateau, so the edge does not depend on hitting an exact value.")
        print(f"\n  To deploy:  tp_runner_lock_below_usd: {tp - best_lock:.2f}")
        print(f"              tp_runner_trail_usd:      {best_trail:.2f}")
        print(f"              tp_runner_arm_before_usd: 1.00   (fixed, never fitted)")


if __name__ == "__main__":
    main()
