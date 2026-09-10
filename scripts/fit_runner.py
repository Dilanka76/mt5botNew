"""Fit the TP-runner from history, measured from the ARM point.

User, 2026-09-09: *"the 5m we need the runner"*.

WHY THE FIRST VERSION OF THIS WAS WRONG. It only looked at trades that
REACHED the target, and scored them against a flat exit there. But the
runner starts acting a dollar earlier, at the ARM point, where it deletes
the broker take-profit. A trade that arms and then turns back before the
target never entered the sample at all -- and that is precisely the case
where the runner does damage. It measured the trades the runner can only
help and skipped every trade it hurts, then reported +$9,335.

That is the same blind spot I had criticised in simulate_tp_runner the
same morning, rebuilt from scratch a few hours later.

WHAT IT DOES NOW. Every trade that reaches the arm point is replayed
TWICE over the identical candles:

  WITH the runner    take-profit gone; the breakeven stop guards the
                     window; at the lock level the stop jumps there and
                     trails.
  WITHOUT it         the take-profit is still sitting at the target; the
                     breakeven stop still guards; nothing else.

The difference between those two IS the runner's contribution, including
every trade where it costs money. Both are replayed under both candle
orderings, since a candle gives a high and a low but not their order:
stop-first assumes the adverse extreme came first (pessimistic),
ratchet-first assumes the favourable one did (optimistic).

PRE-REGISTERED: a setting is worth deploying only if it beats the
runner-off control in BOTH halves and under BOTH orderings, and has
neighbours that also do. "Nothing passes" is a real answer -- on M3 the
live runner is currently $132 behind a control that has none.

arm_before is never fitted: it beats the broker's take-profit fill, which
depends on dollars per SECOND, the same market on every chart.

    python scripts/fit_runner.py --account demo1_m5 --from 2026-03-10 --to 2026-09-08
    python scripts/fit_runner.py --account demo1_m3 --from 2026-03-10 --to 2026-09-08

Read-only.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from simulate_tp_runner import build_context, simulate

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


def armed(df: pd.DataFrame, arm_point: float, stop: float | None) -> list[dict]:
    """Every cross trade that reached the ARM POINT, and the candle it did.

    The arm point, not the target: that is where the runner starts acting,
    by deleting the broker take-profit. Sampling from the target instead
    silently excludes every trade the runner damages.

    Uses the pessimistic within-candle ordering -- the adverse extreme is
    assumed to come first, so a candle that spans both the stop and the
    target counts as stopped, not won. That undercounts winners rather
    than inventing them.
    """
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False           # row 0 has no predecessor
    crosses = list(df.index[changed])
    pos_of = {ts: i for i, ts in enumerate(df.index)}

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
            if favorable >= arm_point:
                out.append({"entry_time": entry_t, "reached_at": idx, "pos": pos_of[idx],
                            "pos": pos_of[idx],
                            "direction": "BUY" if is_buy else "SELL", "entry": entry})
                break
    return out


def replay(ctx: dict, start_pos: int, is_buy: bool, entry: float, *, tp: float,
           lock_level: float | None, trail: float, breakeven_lock: float,
           max_candles: int, ratchet_first: bool) -> tuple[str, float] | None:
    """One trade from the arm point onward, in price dollars of profit.

    lock_level None means WITHOUT the runner: the broker take-profit is
    still at `tp` and closes the trade there. With the runner it has been
    deleted, so the trade only stops at the trailing stop or the opposite
    cross -- and until it reaches lock_level the breakeven stop is all
    there is.
    """
    highs, lows, closes = ctx["high"], ctx["low"], ctx["close"]
    above, changed = ctx["above"], ctx["changed"]
    stop_profit = breakeven_lock          # protected profit, in dollars
    locked = False
    best = 0.0

    for pos in range(start_pos + 1, min(start_pos + 1 + max_candles, len(highs))):
        fav = (highs[pos] - entry) if is_buy else (entry - lows[pos])
        adv = (lows[pos] - entry) if is_buy else (entry - highs[pos])

        def forward() -> str | None:
            nonlocal locked, best, stop_profit
            if lock_level is None:
                return "take-profit" if fav >= tp else None
            if not locked and fav >= lock_level:
                locked, best, stop_profit = True, fav, lock_level
            if locked and fav > best:
                best = fav
                stop_profit = max(stop_profit, best - trail)
            return None

        if ratchet_first:
            hit = forward()
            if hit:
                return (hit, tp)
            if adv <= stop_profit:
                return ("trailed out" if locked else "breakeven", stop_profit)
        else:
            if adv <= stop_profit:
                return ("trailed out" if locked else "breakeven", stop_profit)
            hit = forward()
            if hit:
                return (hit, tp)

        if changed[pos] and bool(above[pos]) != is_buy:
            close_profit = (closes[pos] - entry) if is_buy else (entry - closes[pos])
            return ("opposite cross", close_profit)
    return None


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    args = parse_args()
    config = load_config(args.account)
    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    tp = config.take_profit_usd
    arm_before = float(config.tp_runner_arm_before_usd or 1.0)
    arm_point = tp - arm_before
    breakeven_lock = float(config.breakeven_lock_usd or 0.0)
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
    ctx = build_context(df)

    trades = armed(df, arm_point, config.stop_loss_usd)
    print("=" * 96)
    print(f"FIT THE RUNNER — {args.account} ({config.timeframe})")
    print(f"target ${tp:.2f}   arm point ${arm_point:.2f}   breakeven keeps ${breakeven_lock:.2f}")
    print(f"{args.date_from}..{args.date_to}, {len(df)} candles, {lots} lots")
    print("=" * 96)
    print(f"{len(trades)} trades reached the ARM POINT — every one the runner would touch,")
    print(f"including those that never reached the ${tp:.2f} target.")
    if len(trades) < 30:
        print("FEWER THAN 30. Too few to fit anything.")
        return
    if not trades:
        return
    mid = len(trades) // 2

    # The control: identical candles, take-profit left in place.
    control: dict[str, list[float]] = {}
    for ordering in ("stop-first", "ratchet-first"):
        got = []
        for t in trades:
            r = replay(ctx, t["pos"], t["direction"] == "BUY", t["entry"], tp=tp,
                       lock_level=None, trail=0.0, breakeven_lock=breakeven_lock,
                       max_candles=args.max_candles, ratchet_first=(ordering == "ratchet-first"))
            got.append(r[1] if r else 0.0)
        control[ordering] = got
        print(f"  runner OFF [{ordering:<13}]  ${sum(got) * to_usd:>10,.0f}   "
              f"${sum(got) / len(got) * to_usd:>7.2f}/trade")

    print(f"\n  {'lock at':>8}{'trail':>7}{'ordering':>15}{'vs runner OFF':>15}"
          f"{'1st half':>11}{'2nd half':>11}{'ran':>6}")
    print("-" * 96)
    rows = []
    for lock_below in [float(v) for v in args.locks.split(",")]:
        lock_level = tp - lock_below
        if lock_level <= 0:
            continue
        for trail in [float(v) for v in args.trails.split(",")]:
            for ordering in ("stop-first", "ratchet-first"):
                base = control[ordering]
                got, ran = [], 0
                for t in trades:
                    r = replay(ctx, t["pos"], t["direction"] == "BUY", t["entry"], tp=tp,
                               lock_level=lock_level, trail=trail,
                               breakeven_lock=breakeven_lock, max_candles=args.max_candles,
                               ratchet_first=(ordering == "ratchet-first"))
                    got.append(r[1] if r else 0.0)
                    if r and r[0] == "trailed out" and r[1] > tp:
                        ran += 1
                d = [g - b for g, b in zip(got, base)]
                rows.append({"lock": lock_level, "trail": trail, "ordering": ordering,
                             "delta": sum(d), "d1": sum(d[:mid]), "d2": sum(d[mid:]), "ran": ran})
                print(f"  {lock_level:>8.2f}{trail:>7.2f}{ordering:>15}"
                      f"{sum(d) * to_usd:>+15,.0f}{sum(d[:mid]) * to_usd:>+11,.0f}"
                      f"{sum(d[mid:]) * to_usd:>+11,.0f}{ran:>6}", flush=True)

    print("\n" + "=" * 96)
    print("VERDICT — must beat the runner-off control in BOTH halves and BOTH orderings")
    print("=" * 96)
    pairs: dict[tuple[float, float], dict] = {}
    for r in rows:
        pairs.setdefault((r["lock"], r["trail"]), {})[r["ordering"]] = r
    passing = []
    for key, both in sorted(pairs.items()):
        a, b = both.get("stop-first"), both.get("ratchet-first")
        if a and b and min(a["delta"], b["delta"], a["d1"], a["d2"], b["d1"], b["d2"]) > 0:
            passing.append((key, min(a["delta"], b["delta"])))
    if not passing:
        print("  NOTHING PASSES. No lock/trail beats simply leaving the take-profit in place.")
        print("  Ship with the runner OFF — that is a result, not a failure to find one.")
        return
    passing.sort(key=lambda x: x[1], reverse=True)
    print(f"  {len(passing)} of {len(pairs)} combinations pass. Worst case first:\n")
    for (lock, trail), worst in passing[:8]:
        print(f"    lock +${lock:.2f}, trail ${trail:.2f}   worst ${worst * to_usd:+,.0f}")
    (bl, bt), _ = passing[0]
    near_by = sum(1 for (l, t), _ in passing
                  if abs(l - bl) <= 0.51 and abs(t - bt) <= 0.51 and (l, t) != (bl, bt))
    print(f"\n  Best: lock +${bl:.2f}, trail ${bt:.2f}   neighbours that also pass: {near_by}")
    if near_by < 2:
        print("  WARNING: a lone peak, not a plateau. Do not deploy on it.")
    else:
        print(f"\n  To deploy:  tp_runner_lock_below_usd: {tp - bl:.2f}")
        print(f"              tp_runner_trail_usd:      {bt:.2f}")


if __name__ == "__main__":
    main()
