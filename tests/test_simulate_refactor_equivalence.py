"""The fast simulate() must return exactly what the slow one did.

    python3 tests/test_simulate_refactor_equivalence.py    (needs pandas)

simulate() was rebuilding the EMA-cross state over the whole frame and
boolean-masking the future on EVERY call. Invisible for the 59-trade
study; scripts/fit_runner.py makes ~35,000 calls against 35,000 candles
and it became the entire runtime.

Rewriting a hot loop that decides money is exactly where a silent
behaviour change hides -- an off-by-one in the start position, or the
opposite-cross check reading the wrong row, would shift results without
ever raising. So this reimplements the ORIGINAL logic independently and
compares the two on random walks across every combination of direction,
ordering, lock and trail.
"""
from __future__ import annotations

import random
import sys
import types
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

import pandas as pd

from simulate_tp_runner import build_context, simulate

failures: list[str] = []


def reference(df, start_after, direction, entry, lock, trail, max_candles, ratchet_first):
    """The pre-refactor implementation, written out independently."""
    future = df[df.index > start_after]
    if future.empty:
        return None
    is_buy = direction == "BUY"
    stop, best = lock, lock
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False

    for i, (idx, row) in enumerate(future.iterrows()):
        if i >= max_candles:
            return None
        adverse = float(row["low"]) if is_buy else float(row["high"])
        adverse_profit = (adverse - entry) if is_buy else (entry - adverse)
        favorable = float(row["high"]) if is_buy else float(row["low"])
        fav_profit = (favorable - entry) if is_buy else (entry - favorable)

        def ratchet():
            nonlocal best, stop
            if fav_profit > best:
                best = fav_profit
                if trail is not None:
                    stop = max(stop, best - trail)

        if ratchet_first:
            ratchet()
            if adverse_profit <= stop:
                return ("stopped at lock" if stop <= lock else "trailed out", stop)
        else:
            if adverse_profit <= stop:
                return ("stopped at lock" if stop <= lock else "trailed out", stop)
            ratchet()

        if bool(changed.loc[idx]) and bool(above.loc[idx]) != is_buy:
            close_profit = (float(row["close"]) - entry) if is_buy else (entry - float(row["close"]))
            return ("opposite cross", close_profit)
    return None


def random_frame(rng, n=120):
    price, rows = 100.0, []
    for _ in range(n):
        price += rng.uniform(-1.5, 1.5)
        hi = price + rng.uniform(0, 2.0)
        lo = price - rng.uniform(0, 2.0)
        rows.append((price, hi, lo, rng.random(), rng.random()))
    idx = pd.date_range("2026-01-01T00:00Z", periods=n, freq="3min")
    return pd.DataFrame({"close": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "open": [r[0] for r in rows],
                         "ema13": [r[3] for r in rows], "ema21": [r[4] for r in rows]}, index=idx)


def main() -> None:
    print("fast simulate() vs the original logic")
    rng = random.Random(20260909)
    compared = mismatched = 0

    for trial in range(40):
        df = random_frame(rng)
        ctx = build_context(df)
        for start_i in (0, 5, 37, len(df) - 2, len(df) - 1):
            start_after = df.index[start_i]
            for direction in ("BUY", "SELL"):
                entry = float(df["close"].iloc[start_i])
                for lock in (0.5, 5.0):
                    for trail in (None, 0.25, 1.0):
                        for ratchet_first in (False, True):
                            want = reference(df, start_after, direction, entry, lock,
                                             trail, 300, ratchet_first)
                            got = simulate(df, start_after, direction, entry, lock, trail,
                                           300, None, ratchet_first, ctx=ctx,
                                           start_pos=start_i)
                            compared += 1
                            if want is None or got is None:
                                same = want is got
                            else:
                                same = (want[0] == got[0] and abs(want[1] - got[1]) < 1e-9)
                            if not same:
                                mismatched += 1
                                if mismatched <= 3:
                                    print(f"        trial {trial} start {start_i} {direction} "
                                          f"lock {lock} trail {trail} ratchet {ratchet_first}: "
                                          f"want {want} got {got}")

    print(f"  {'OK  ' if not mismatched else 'FAIL'}  {compared:,} comparisons, "
          f"{mismatched} mismatches")
    if mismatched:
        failures.append("results differ")

    # The context must be optional -- the real-trade caller passes none.
    df = random_frame(rng)
    a = simulate(df, df.index[3], "BUY", float(df["close"].iloc[3]), 1.0, 0.5, 300)
    b = simulate(df, df.index[3], "BUY", float(df["close"].iloc[3]), 1.0, 0.5, 300,
                 ctx=build_context(df), start_pos=3)
    print(f"  {'OK  ' if a == b else 'FAIL'}  same answer with and without a prebuilt context")
    if a != b:
        failures.append("context changes the answer")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
