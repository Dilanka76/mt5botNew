"""A backtest must actually trade.

    python3 tests/test_backtest_warmup.py
    (needs pandas and .env.demo2_m3 -- run it on the server)

REAL INCIDENT, 2026-09-24. The first long backtest since the warm-up
guard landed (commit 35dfc72, 2026-09-17) reported **0 trades over a
full year** on both demo2 legs, with 10,000 candles a month present in
the fetch log.

The cause: the live warm-up guard refuses entries until one real candle
period of WALL-CLOCK time has passed since the engine was built, so a
restart cannot fire on a cross that closed while the bot was down. A
year of M3 replays in about 90 seconds -- less than one 3-minute
candle -- so `_warmed_up()` was False for the entire run and every
single entry was silently refused. Nothing raised. The report printed a
clean, confident, completely empty table.

That is the dangerous shape of bug: a research tool that answers "this
strategy never trades" instead of failing. This test makes a replay that
MUST produce trades, so the same silence cannot come back.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
_stub.__getattr__ = lambda name: 0                    # type: ignore[attr-defined]
sys.modules.setdefault("MetaTrader5", _stub)

import math                                            # noqa: E402
from dataclasses import replace                        # noqa: E402

import pandas as pd                                    # noqa: E402

from bot.backtest.runner import run_backtest           # noqa: E402
from bot.config import load_config                     # noqa: E402
from bot.indicators.ema import compute_emas            # noqa: E402

failures: list[str] = []
CANDLES = 1200
BAR = timedelta(minutes=3)
START = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def zigzag() -> pd.DataFrame:
    """Gold walking up and down in clean $16 swings, slow enough that
    EMA13 and EMA21 genuinely cross and far enough that a $6 target is
    reachable. Nothing subtle -- any working engine must trade this."""
    rows, times = [], []
    for i in range(CANDLES):
        mid = 4300.0 + 8.0 * math.sin(i / 19.0)
        nxt = 4300.0 + 8.0 * math.sin((i + 1) / 19.0)
        rows.append({"open": mid, "close": nxt,
                     "high": max(mid, nxt) + 0.4, "low": min(mid, nxt) - 0.4,
                     "tick_volume": 250, "spread": 12, "real_volume": 0})
        times.append(START + i * BAR)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(times))


print("a replay of 1,200 candles with real EMA crosses")
config = load_config("demo2_m3")
# Only the base rule is under test here: the optional layers each need
# their own higher-timeframe frame, and a missing column would fail this
# test for a reason that has nothing to do with the warm-up guard.
config = replace(config, htf_trend_timeframe=None, htf_trend_take_profit_usd=None,
                 range_filter=None, consolidation_filter=None)
df = compute_emas(zigzag(), config.ema_periods)
crosses = int(((df["ema13"] > df["ema21"]) != (df["ema13"] > df["ema21"]).shift(1)).iloc[1:].sum())
check(f"the test data really does cross ({crosses} times)", crosses >= 10)

trades = run_backtest(config, df, START + 60 * BAR, contract_size=100.0, point=0.01,
                      starting_balance=300.0)
check(f"the backtest produced trades ({len(trades)})", len(trades) > 0)
check("every trade has a profit and a close time",
      all("profit" in t and "close_time" in t for t in trades))
check("it did not just open one and stop", len(trades) >= 5)

print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
raise SystemExit(1 if failures else 0)
