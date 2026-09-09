"""The M15 trend column must never read an unfinished candle.

    python3 tests/test_htf_trend.py       (needs pandas)

demo2_m3's rule (user, 2026-09-09) picks a $8 target instead of $6 when a
trade runs WITH the M15 EMA13/21 trend. MT5 indexes a candle by the time
it OPENED, so the M15 row at 10:00 is not finished until 10:15. Reading
it at 10:06 would let a backtest see nine minutes into the future and
score a rule the live bot could never run -- the same class of fault as
the wrong-candle bug that invalidated a week of research
(bot/strategy/cross_lookup.py).

So the checks below are mostly about TIME, not about EMAs.
"""
from __future__ import annotations

import sys
import types

sys.path.insert(0, ".")
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

import pandas as pd

from bot.config import EMAPeriodsConfig
from bot.indicators.htf_trend import agrees_with_trend, compute_htf_trend

PERIODS = EMAPeriodsConfig(fast=5, mid=13, slow=21)
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def candles(start: str, periods: int, freq: str, prices) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    return pd.DataFrame({"open": prices, "high": [p + 1 for p in prices],
                         "low": [p - 1 for p in prices], "close": prices}, index=idx)


def main() -> None:
    print("higher-timeframe trend column")

    # M15 rises for 40 candles then falls hard for 40 -- a clean flip.
    up = [100.0 + i for i in range(40)]
    down = [140.0 - 3 * i for i in range(40)]
    htf = candles("2026-09-09T00:00Z", 80, "15min", up + down)
    m3 = candles("2026-09-09T00:00Z", 400, "3min", [100.0] * 400)

    out = compute_htf_trend(m3, htf, 15, PERIODS)
    check("adds an htf_trend column", "htf_trend" in out.columns)
    check("leaves the source frame untouched", "htf_trend" not in m3.columns)
    check("same number of rows as the trading timeframe", len(out) == len(m3))

    # THE important one. The first M15 candle opens 00:00 and closes
    # 00:15, so nothing before 00:15 may have a value.
    before = out.loc[out.index < pd.Timestamp("2026-09-09T00:15Z"), "htf_trend"]
    check("no value before the first M15 candle has CLOSED", before.isna().all())

    # A value that appears at 00:15 must come from the 00:00 candle, not
    # the 00:15 one, which is still forming.
    htf_marked = htf.copy()
    trend_at_0015 = out.loc[pd.Timestamp("2026-09-09T00:15Z"), "htf_trend"]
    check("a value exists exactly at the first close", trend_at_0015 == trend_at_0015)

    # Shift the whole M15 history one period later: every value on the
    # trading timeframe must move by exactly one period too. If the code
    # were reading open times, this would not hold.
    shifted = htf_marked.copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=15)
    out2 = compute_htf_trend(m3, shifted, 15, PERIODS)
    a = out["htf_trend"].dropna()
    b = out2["htf_trend"].dropna()
    check("shifting the M15 history shifts the column with it",
          len(b) < len(a) and b.index[0] == a.index[0] + pd.Timedelta(minutes=15))

    # The trend must actually flip somewhere once M15 turns down.
    values = set(out["htf_trend"].dropna().unique())
    check("both an uptrend and a downtrend are produced", values == {1.0, -1.0})

    # No higher-timeframe data at all -> all NaN, never a guess.
    empty = compute_htf_trend(m3, pd.DataFrame(), 15, PERIODS)
    check("no M15 data gives NaN, not a default direction",
          empty["htf_trend"].isna().all())

    print("\nagrees_with_trend")
    check("BUY with an uptrend agrees", agrees_with_trend("BUY", 1.0) is True)
    check("SELL with a downtrend agrees", agrees_with_trend("SELL", -1.0) is True)
    check("BUY against a downtrend does not", agrees_with_trend("BUY", -1.0) is False)
    check("SELL against an uptrend does not", agrees_with_trend("SELL", 1.0) is False)
    check("an UNKNOWN trend never agrees (falls back to the normal target)",
          agrees_with_trend("BUY", float("nan")) is False
          and agrees_with_trend("BUY", None) is False)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
