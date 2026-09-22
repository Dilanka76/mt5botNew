"""Does the bot lose money on crosses taken INSIDE a trader-drawn range?

User, 2026-09-21: "the main research is find the consolidation range, we
need to skip it, then we can take [the good] trades." Lot sizes stay
exactly as they are -- this is about which trades, not how big.

Three earlier measures of "box" failed on past data (M15 candle overlap,
M15 box height, the H1 parent candle): trades inside them still made
money. All three measured a box the way an INDICATOR does. A trader draws
a range differently -- a flat ceiling and a flat floor, each touched at
least twice -- and that has never been tested here. This is that test.

THE DEFINITION, frozen 2026-09-21 before any result was seen:

  On the M15 chart, over the last 16 CLOSED candles before entry (4 hours):
    * swing highs and lows are fractals: a candle whose high (low) beats
      the two candles on each side -- only swings already confirmed by
      the entry moment count
    * CEILING: two swing highs within 0.35 x M15 ATR of each other
    * FLOOR:   two swing lows  within 0.35 x M15 ATR of each other
    * IN RANGE: both exist, the ceiling is clearly above the floor, and
      the entry price sits between them

THE RULE, frozen at the same time:

  Each account is judged on its own, on trades from 2026-09-22 00:00 UTC
  onward ONLY. The in-range group counts as a real finding only if it
  LOSES money ($/oz below zero) in BOTH halves of that window, with at
  least 20 in-range trades. "Makes less but still makes money" means
  those trades keep being taken -- skipping them would cost money.

WHY FROM 2026-09-22 AND NOT TODAY: this definition was written after the
evening of 2026-09-21 had been looked at (a $9 whipsaw, -$97 on M3). A
rule shaped with any trade in view must not be scored on that trade.

    python scripts/range_forward_test.py
    python scripts/range_forward_test.py --offset-hours 3      (weekends)

Read-only.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0
HTF = "M15"
HTF_MINUTES = 15
LOOKBACK = 16            # M15 candles = 4 hours
FRACTAL = 2              # candles each side of a swing
LEVEL_TOLERANCE = 0.35   # x ATR: two touches this close are "the same level"
MIN_IN_RANGE = 20
FORWARD_START = "2026-09-22 00:00:00"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5,live2_m3,live2_m5")
    p.add_argument("--since", default=FORWARD_START,
                   help="true UTC. The frozen forward window starts at " + FORWARD_START)
    p.add_argument("--until", default=None,
                   help="true UTC, exclusive. For the BACKWARD test: '2026-09-21 12:00:00' "
                        "keeps out the evening the definition was written after.")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def with_atr(htf: pd.DataFrame) -> pd.DataFrame:
    out = htf.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat([out["high"] - out["low"],
                    (out["high"] - prev_close).abs(),
                    (out["low"] - prev_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return out


def paired_level(values: list, tol: float, pick) -> float | None:
    """The most recent pair of swings within `tol` of each other; returns
    `pick` of the pair (max for a ceiling, min for a floor)."""
    for i in range(len(values) - 1, 0, -1):
        for j in range(i - 1, -1, -1):
            if abs(values[i] - values[j]) <= tol:
                return pick(values[i], values[j])
    return None


def range_at(htf: pd.DataFrame, entry_utc: datetime, price: float):
    """(in_range, ceiling, floor), or None when there is too little history."""
    closed = htf[htf.index + timedelta(minutes=HTF_MINUTES) <= entry_utc].tail(LOOKBACK)
    if len(closed) < LOOKBACK:
        return None
    atr = float(closed["atr"].iloc[-1])
    if not atr or atr != atr:
        return None
    tol = LEVEL_TOLERANCE * atr
    highs, lows = [], []
    hi, lo = closed["high"].tolist(), closed["low"].tolist()
    # a fractal needs FRACTAL candles after it, all already closed
    for i in range(FRACTAL, len(closed) - FRACTAL):
        window_h = hi[i - FRACTAL:i + FRACTAL + 1]
        window_l = lo[i - FRACTAL:i + FRACTAL + 1]
        if hi[i] == max(window_h):
            highs.append(hi[i])
        if lo[i] == min(window_l):
            lows.append(lo[i])
    ceiling = paired_level(highs, tol, max)
    floor = paired_level(lows, tol, min)
    if ceiling is None or floor is None or ceiling <= floor + tol:
        return False, ceiling, floor
    return floor <= price <= ceiling, ceiling, floor


def report(label: str, rows: list, order: dict, half: int) -> tuple:
    if not rows:
        print(f"    {label:<24} none")
        return 0, float("nan"), float("nan")
    w = sum(1 for r in rows if r["oz"] > 0)
    f = mean([r["oz"] for r in rows if order[id(r)] < half])
    s = mean([r["oz"] for r in rows if order[id(r)] >= half])
    print(f"    {label:<24} {len(rows):>4} trades  {100 * w / len(rows):>4.0f}% won  "
          f"{mean([r['oz'] for r in rows]):>+6.2f} $/oz   halves {f:>+6.2f} / {s:>+6.2f}   "
          f"{money(sum(r['profit'] for r in rows)):>11}")
    return len(rows), f, s


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    until = (datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
             if args.until else None)

    print("=" * 104)
    print("RANGE TEST -- crosses inside a trader-drawn M15 range (ceiling + floor, each touched twice)")
    print(f"trades since {since:%Y-%m-%d %H:%M} UTC"
          + (f" until {until:%Y-%m-%d %H:%M} UTC" if until else "")
          + ".  Outcome in $/oz, so lot size tilts nothing.")
    written = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    if since < datetime.strptime(FORWARD_START, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc):
        if until is not None and until <= written:
            # Fair, and said why: the definition and the pass rule were fixed
            # on 2026-09-21 without ever being run on these trades. What must
            # stay out is only what was looked at while writing it.
            print("BACKWARD TEST: the definition was frozen 2026-09-21 without being run on these")
            print("trades, so they are a fair test of it. The evening it was written after is excluded.")
        else:
            print("*** this window reaches past 2026-09-21 12:00 UTC, into trades that were SEEN")
            print("*** while the definition was written. Use --until \"2026-09-21 12:00:00\".")
    print("=" * 104)

    verdicts = {}
    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            htf = get_ohlc_range(connector, config.symbol, HTF,
                                 since - timedelta(days=2), now, offset)
        finally:
            connector.disconnect()
        htf = with_atr(htf)

        rows, unknown = [], 0
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since or (until is not None and entry_utc >= until):
                continue
            got = range_at(htf, entry_utc, float(t["entry_price"]))
            if got is None:
                unknown += 1
                continue
            rows.append({"entry_utc": entry_utc, "profit": float(t["profit"]),
                         "oz": float(t["profit"]) / (float(t["volume"]) * OZ_PER_LOT),
                         "in_range": got[0]})

        print(f"\n{account}   {config.timeframe}   {len(rows)} trades"
              + (f"   ({unknown} without 4h of M15 history, left out)" if unknown else ""))
        if not rows:
            print("    no trades in the window yet")
            verdicts[account] = "NO TRADES YET"
            continue
        order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry_utc"]))}
        half = len(rows) // 2
        inside = [r for r in rows if r["in_range"]]
        print(f"    {100 * len(inside) / len(rows):.0f}% of entries were inside a range")
        report("outside a range", [r for r in rows if not r["in_range"]], order, half)
        n, f, s = report("INSIDE a range", inside, order, half)

        if n < MIN_IN_RANGE:
            verdict = f"WAIT -- only {n} in-range trades so far, need {MIN_IN_RANGE}"
        elif f < 0 and s < 0:
            verdict = "in-range trades LOSE in both halves -- PASSES, skipping them would gain"
        elif f < 0 or s < 0:
            verdict = "loses in one half only -- fails"
        else:
            verdict = "in-range trades still PROFITABLE -- fails; keep taking them"
        verdicts[account] = verdict
        print(f"    VERDICT: {verdict}")

    print(f"\n{'=' * 104}")
    print("RULE (frozen 2026-09-21): in-range trades must LOSE money in BOTH halves, with at least")
    print(f"{MIN_IN_RANGE} of them, on trades from {FORWARD_START} UTC onward. Each account on its own.")
    for account, verdict in verdicts.items():
        print(f"  {account:<10} {verdict}")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
