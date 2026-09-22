"""Is price inside a trader-drawn range on the higher timeframe?

User, 2026-09-21: "the main research is find the consolidation range, we
need to skip it." Three indicator-style box measures failed on past data.
This one draws the range the way a trader does -- a flat ceiling and a
flat floor, each touched at least twice -- and on the first backward test
(2026-09-22, frozen definition, 2026-08-25..09-21) entries inside it were
WORSE than entries outside on all six accounts, losing money on five:
demo2_m3 15 trades -$1.96/oz vs +$0.93 outside. Too few to prove it, so
it ships switched off, and runs record-only on live2 / skipping on demo2
while forward trades settle it.

THE DEFINITION (frozen 2026-09-21, identical to scripts/range_forward_test.py,
which now imports range_levels from here so the two can never drift):

  over the last `lookback` CLOSED higher-timeframe candles (16 x M15 = 4h):
    swing highs / lows are fractals: a candle whose high (low) beats the
      `fractal` candles on each side -- only swings already confirmed
    CEILING = the most recent pair of swing highs within tol of each other
    FLOOR   = the most recent pair of swing lows  within tol of each other
    tol     = level_tolerance_atr x the higher timeframe's ATR(14)
    a RANGE exists when both do and the ceiling is clearly above the floor

NO HINDSIGHT: each higher-timeframe candle's verdict is stamped at the
moment it CLOSES and carried onto the trading candles by THEIR close time
-- so a trading candle only ever sees ranges built from candles that had
finished by the time it did.
"""
from __future__ import annotations

import pandas as pd

ATR_PERIOD = 14


def with_atr(htf: pd.DataFrame) -> pd.DataFrame:
    """A copy with an "atr" column: Wilder-style ATR(14)."""
    out = htf.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat([out["high"] - out["low"],
                    (out["high"] - prev_close).abs(),
                    (out["low"] - prev_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()
    return out


def _paired_level(values: list, tol: float, pick):
    """The most recent pair of swings within `tol` of each other."""
    for i in range(len(values) - 1, 0, -1):
        for j in range(i - 1, -1, -1):
            if abs(values[i] - values[j]) <= tol:
                return pick(values[i], values[j])
    return None


def range_levels(window: pd.DataFrame, fractal: int = 2, level_tolerance_atr: float = 0.35):
    """(ceiling, floor) of the range in `window` -- CLOSED candles with an
    "atr" column, oldest first -- or (None, None) when there is no range.
    Raises nothing; an unusable ATR also means no range."""
    return _levels(window["high"].tolist(), window["low"].tolist(),
                   float(window["atr"].iloc[-1]), fractal, level_tolerance_atr)


def _levels(hi: list, lo: list, atr: float, fractal: int, level_tolerance_atr: float):
    """range_levels on plain lists -- the live loop calls this every second,
    so it must not slice DataFrames."""
    if not atr or atr != atr:
        return None, None
    tol = level_tolerance_atr * atr
    highs, lows = [], []
    # a fractal needs `fractal` candles after it, all inside the window
    for i in range(fractal, len(hi) - fractal):
        if hi[i] == max(hi[i - fractal:i + fractal + 1]):
            highs.append(hi[i])
        if lo[i] == min(lo[i - fractal:i + fractal + 1]):
            lows.append(lo[i])
    ceiling = _paired_level(highs, tol, max)
    floor = _paired_level(lows, tol, min)
    if ceiling is None or floor is None or ceiling <= floor + tol:
        return None, None
    return ceiling, floor


def compute_range(df: pd.DataFrame, htf_df: pd.DataFrame, htf_minutes: int, bar_minutes: int,
                  lookback: int = 16, fractal: int = 2,
                  level_tolerance_atr: float = 0.35) -> pd.DataFrame:
    """A copy of `df` with range_state (1.0 = a range exists, 0.0 = none,
    NaN = not enough history), range_ceiling and range_floor, as of the
    higher-timeframe candles CLOSED by each trading candle's close."""
    out = df.copy()
    if htf_df is None or htf_df.empty or len(htf_df) < lookback:
        out["range_state"] = float("nan")
        out["range_ceiling"] = float("nan")
        out["range_floor"] = float("nan")
        return out

    h = with_atr(htf_df)
    hi, lo, atr = h["high"].tolist(), h["low"].tolist(), h["atr"].tolist()
    states, ceilings, floors = [], [], []
    for i in range(len(h)):
        if i + 1 < lookback:
            states.append(float("nan"))
            ceilings.append(float("nan"))
            floors.append(float("nan"))
            continue
        a = i + 1 - lookback
        ceiling, floor = _levels(hi[a:i + 1], lo[a:i + 1], atr[i], fractal, level_tolerance_atr)
        states.append(1.0 if ceiling is not None else 0.0)
        ceilings.append(ceiling if ceiling is not None else float("nan"))
        floors.append(floor if floor is not None else float("nan"))

    stamped_at = h.index + pd.Timedelta(minutes=htf_minutes)      # when each candle CLOSED
    close_times = out.index + pd.Timedelta(minutes=bar_minutes)    # when each trading candle closed
    for name, values in (("range_state", states), ("range_ceiling", ceilings),
                         ("range_floor", floors)):
        series = pd.Series(values, index=stamped_at, dtype="float64")
        series = series[~series.index.duplicated(keep="last")].sort_index()
        out[name] = series.reindex(close_times, method="ffill").to_numpy()
    return out


def in_range(price, state, ceiling, floor, filter_config) -> bool | None:
    """True = the entry would be inside a range. False = it would not.
    None = cannot tell (filter off, no history, a NaN) -- which the engine
    treats as "trade exactly as before". Failing OPEN is deliberate."""
    if filter_config is None or not filter_config.enabled:
        return None
    try:
        p, s = float(price), float(state)
    except (TypeError, ValueError):
        return None
    if p != p or s != s:
        return None
    if s < 0.5:
        return False
    try:
        c, f = float(ceiling), float(floor)
    except (TypeError, ValueError):
        return None
    if c != c or f != f:
        return None
    return f <= p <= c
