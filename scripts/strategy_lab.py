"""STRATEGY LAB -- different families of strategy, one honest scoreboard.

2026-09-24. The EMA13/21 family is finished: 3 entry shapes, 4 exit
rules, 4 timeframes, 31 filters, a year of data each, everything
negative. Every one of those was a variation of the SAME rule. So this
stops varying it and tests families that are genuinely different, each
against the identical bar.

WHAT IS IN HERE, with every parameter fixed at its conventional value so
nothing is tuned to this data:

  orb        OPENING RANGE BREAKOUT. The first hour of a session sets a
             high and a low; a candle closing outside it enters that way.
             Stop: the opposite side of the range. Target: twice the
             range height. One trade per day, per direction.
  donchian   THE TURTLE RULE, the most documented trend system there is.
             Buy a break of the highest high of the last 20 candles, sell
             the lowest low. Stop 2 ATR. Held until the opposite break.
  bollinger  MEAN REVERSION. A close outside the 2-sigma band is faded.
             Target: the middle band. Stop 2 ATR.
  rsi        RSI(14) below 30 buys, above 70 sells. Exit when RSI crosses
             back through 50. Stop 2 ATR.
  nr7        VOLATILITY CONTRACTION. The narrowest candle of the last 7
             sets a high and a low; the first break within 3 candles
             trades that way. Stop: the other side of that candle.
             Target: twice its height. A different mechanism again --
             quiet before a move, rather than a trend or a level.

HOW EVERY ONE IS SCORED, identically:
  - sequential, ONE position at a time, exactly as a real account works
  - a signal confirms at a candle's CLOSE and enters at the NEXT open
  - every trade pays 0.18 $/oz (a $6/lot commission plus a $0.12 spread)
  - a candle touching both stop and target counts as the STOP
  - results in $/oz, so lot size tilts nothing

THE BAR, the same one that has failed 31 ideas:
  1. profitable after costs over the whole period,
  2. profitable in BOTH halves,
  3. at least 100 trades,
  4. a drawdown a small account could live through.

AND THE RULE ABOUT LUCK: testing several strategies means roughly one in
four or five passes by chance alone. Anything that passes here is NOT a
result yet -- it must then pass on a period it has never seen, and then
three weeks forward on demo. That is what steps 5 and 6 of the plan are
for, and this script prints the reminder every time.

    python scripts/strategy_lab.py --strategy donchian --timeframe H1 --since "2025-09-01 00:00:00"
    python scripts/strategy_lab.py --strategy orb --timeframe M15 --since "2025-09-01 00:00:00"
    python scripts/strategy_lab.py --strategy bollinger --timeframe M15
    python scripts/strategy_lab.py --strategy rsi --timeframe M15
    (add --offset-hours 3 when the market is closed)

Read-only. No engine, no config, no bot, no order.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import OrderedDict
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

COSTS_PER_OZ = 0.18
MIN_TRADES = 100
ATR_PERIOD = 14

# Conventional settings, fixed before any run. Changing one of these to
# improve a result is tuning, and tuning is what put real money at risk
# in August -- see the project's memory files.
DONCHIAN_LOOKBACK = 20
BOLLINGER_PERIOD, BOLLINGER_SIGMA = 20, 2.0
RSI_PERIOD, RSI_LOW, RSI_HIGH, RSI_EXIT = 14, 30.0, 70.0, 50.0
STOP_ATRS = 2.0
ORB_SESSION_START = time(7, 0)      # London, UTC
ORB_RANGE_MINUTES = 60
ORB_TARGET_MULTIPLE = 2.0
NR7_LOOKBACK, NR7_VALID_CANDLES, NR7_TARGET_MULTIPLE = 7, 3, 2.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True,
                   choices=("orb", "donchian", "bollinger", "rsi", "nr7"))
    p.add_argument("--account", default="demo2_m3", help="only for the symbol and connection")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--since", default="2025-09-01 00:00:00", help="true UTC")
    p.add_argument("--until", default=None)
    p.add_argument("--max-hold-hours", type=float, default=72.0)
    p.add_argument("--orb-session-start", default="07:00",
                   help="UTC start of the hour that sets the range. 07:00 (London) is the "
                        "PRE-REGISTERED one; 12:00 (New York) exists as an independent check "
                        "that the edge is a session effect and not one lucky hour. Changing "
                        "this until a number improves is tuning, not testing.")
    p.add_argument("--offset-hours", type=float, default=None)
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def add_indicators(df):
    """Everything a strategy might read, all backward-looking. A rolling
    window in pandas ends at the current candle, and anything that must
    exclude the current candle is shifted explicitly."""
    import pandas as pd

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prev_close).abs(),
                            (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1 / ATR_PERIOD, adjust=False).mean()

    # Donchian: the highest high of the PREVIOUS n candles, so a break of
    # it on this candle is a real break and not a comparison with itself.
    df["dc_high"] = high.rolling(DONCHIAN_LOOKBACK).max().shift(1)
    df["dc_low"] = low.rolling(DONCHIAN_LOOKBACK).min().shift(1)

    mid = close.rolling(BOLLINGER_PERIOD).mean()
    sd = close.rolling(BOLLINGER_PERIOD).std()
    df["bb_mid"] = mid
    df["bb_up"] = mid + BOLLINGER_SIGMA * sd
    df["bb_dn"] = mid - BOLLINGER_SIGMA * sd

    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, float("nan")))
    return df


def signals_orb(df, session_start=ORB_SESSION_START):
    """The first hour of the London session sets the day's range; the
    first close outside it trades that way, once per day per side."""
    import pandas as pd

    out = [None] * len(df)
    idx = list(df.index)
    hi, lo, cl = df["high"].tolist(), df["low"].tolist(), df["close"].tolist()
    end = (datetime.combine(datetime(2000, 1, 1), session_start)
           + timedelta(minutes=ORB_RANGE_MINUTES)).time()

    day_range: dict = {}
    fired: dict = {}
    for i, ts in enumerate(idx):
        day = ts.date()
        t = ts.time()
        if session_start <= t < end:
            h, l = day_range.get(day, (float("-inf"), float("inf")))
            day_range[day] = (max(h, hi[i]), min(l, lo[i]))
            continue
        if t < end or day not in day_range:
            continue
        top, bottom = day_range[day]
        height = top - bottom
        if height <= 0:
            continue
        if cl[i] > top and ("up", day) not in fired:
            fired[("up", day)] = True
            out[i] = (1, cl[i] - bottom, ORB_TARGET_MULTIPLE * height)
        elif cl[i] < bottom and ("dn", day) not in fired:
            fired[("dn", day)] = True
            out[i] = (-1, top - cl[i], ORB_TARGET_MULTIPLE * height)
    return out


def signals_donchian(df):
    out = [None] * len(df)
    hi, lo = df["high"].tolist(), df["low"].tolist()
    dch, dcl, atr = df["dc_high"].tolist(), df["dc_low"].tolist(), df["atr"].tolist()
    for i in range(len(df)):
        a = atr[i]
        if a != a or dch[i] != dch[i]:
            continue
        if hi[i] > dch[i]:
            out[i] = (1, STOP_ATRS * a, None)         # no target: trend following
        elif lo[i] < dcl[i]:
            out[i] = (-1, STOP_ATRS * a, None)
    return out


def signals_bollinger(df):
    out = [None] * len(df)
    cl, up, dn, mid = (df["close"].tolist(), df["bb_up"].tolist(),
                       df["bb_dn"].tolist(), df["bb_mid"].tolist())
    atr = df["atr"].tolist()
    for i in range(len(df)):
        a = atr[i]
        if a != a or up[i] != up[i]:
            continue
        if cl[i] > up[i]:
            out[i] = (-1, STOP_ATRS * a, max(cl[i] - mid[i], 0.01))
        elif cl[i] < dn[i]:
            out[i] = (1, STOP_ATRS * a, max(mid[i] - cl[i], 0.01))
    return out


def signals_rsi(df):
    out = [None] * len(df)
    r, atr = df["rsi"].tolist(), df["atr"].tolist()
    for i in range(len(df)):
        a, v = atr[i], r[i]
        if a != a or v != v:
            continue
        if v < RSI_LOW:
            out[i] = (1, STOP_ATRS * a, None)
        elif v > RSI_HIGH:
            out[i] = (-1, STOP_ATRS * a, None)
    return out


def signals_nr7(df):
    """The narrowest candle of the last 7 is a market holding its breath.
    The first break of that candle, within the next 3, is the trade."""
    out = [None] * len(df)
    hi, lo = df["high"].tolist(), df["low"].tolist()
    setup = None                      # (high, low, candles_left)
    for i in range(len(df)):
        if setup is not None:
            top, bottom, left = setup
            height = top - bottom
            if hi[i] > top:
                out[i] = (1, hi[i] - bottom, NR7_TARGET_MULTIPLE * height)
                setup = None
            elif lo[i] < bottom:
                out[i] = (-1, top - lo[i], NR7_TARGET_MULTIPLE * height)
                setup = None
            elif left <= 1:
                setup = None
            else:
                setup = (top, bottom, left - 1)
        if i + 1 < NR7_LOOKBACK:
            continue
        ranges = [hi[j] - lo[j] for j in range(i + 1 - NR7_LOOKBACK, i + 1)]
        if ranges[-1] <= min(ranges) and ranges[-1] > 0 and setup is None:
            setup = (hi[i], lo[i], NR7_VALID_CANDLES)
    return out


SIGNALS = {"orb": signals_orb, "donchian": signals_donchian,
           "bollinger": signals_bollinger, "rsi": signals_rsi, "nr7": signals_nr7}


def simulate(df, signals, strategy, max_hold):
    """One position at a time. A signal confirms at a candle's CLOSE and is
    entered at the NEXT candle's open, so nothing reads a price the rule
    could not have seen. A candle touching both stop and target is scored
    as the STOP."""
    o, hi, lo, cl = (df["open"].tolist(), df["high"].tolist(),
                     df["low"].tolist(), df["close"].tolist())
    rsi = df["rsi"].tolist()
    times = list(df.index)
    trades: list = []
    position = None

    # From candle 0: unlike a cross rule, a precomputed signal can sit on
    # the very first candle, and starting at 1 silently dropped it
    # (caught by tests/test_strategy_lab.py before any result was read).
    for i in range(len(df)):
        if position is not None:
            sign, stop, tp = position["sign"], position["stop"], position["tp"]
            hit_stop = (lo[i] <= stop) if sign > 0 else (hi[i] >= stop)
            hit_tp = tp is not None and ((hi[i] >= tp) if sign > 0 else (lo[i] <= tp))
            # A trend system has no target: it turns on the opposite
            # signal. RSI closes when the market is no longer stretched.
            flip = False
            if strategy == "donchian" and signals[i] is not None:
                flip = signals[i][0] != sign
            elif strategy == "rsi":
                v = rsi[i]
                flip = v == v and ((v >= RSI_EXIT) if sign > 0 else (v <= RSI_EXIT))
            out = why = None
            if hit_stop:
                out, why = stop, "stop"
            elif hit_tp:
                out, why = tp, "target"
            elif flip:
                out, why = cl[i], "signal"
            elif times[i] - position["opened"] >= max_hold:
                out, why = cl[i], "max hold"
            if out is None:
                continue
            trades.append({"opened": position["opened"], "closed": times[i],
                           "oz": (out - position["entry"]) * sign - COSTS_PER_OZ,
                           "reason": why, "direction": "BUY" if sign > 0 else "SELL"})
            position = None

        sig = signals[i]
        if sig is None or i + 1 >= len(df) or position is not None:
            continue
        sign, stop_distance, target_distance = sig
        if not stop_distance or stop_distance <= 0:
            continue
        entry = o[i + 1]
        position = {"sign": sign, "entry": entry, "opened": times[i + 1],
                    "stop": entry - sign * stop_distance,
                    "tp": (entry + sign * target_distance) if target_distance else None}
    return trades


def report(trades, title, lines):
    print("=" * 92)
    print(title)
    for line in lines:
        print(line)
    print("=" * 92)
    if not trades:
        print("\n  no trades")
        return
    half = len(trades) // 2
    first, second = [t["oz"] for t in trades[:half]], [t["oz"] for t in trades[half:]]
    wins = [t for t in trades if t["oz"] > 0]
    losses = [t["oz"] for t in trades if t["oz"] <= 0]
    total = sum(t["oz"] for t in trades)

    print(f"\n  trades              {len(trades)}")
    print(f"  won                 {len(wins)} ({100 * len(wins) / len(trades):.1f}%)")
    print(f"  net                 {mean([t['oz'] for t in trades]):+.3f} $/oz per trade, "
          f"{total:+.2f} $/oz in total")
    print(f"  at 0.01 lots        {money(total)}")
    print(f"  halves              {mean(first):+.3f} / {mean(second):+.3f} $/oz")
    if wins and losses:
        print(f"  average win / loss  {mean([t['oz'] for t in wins]):+.2f} / {mean(losses):+.2f}"
              f"   ratio {abs(mean([t['oz'] for t in wins]) / mean(losses)):.2f}")
    for reason in ("target", "stop", "signal", "max hold"):
        sel = [t for t in trades if t["reason"] == reason]
        if sel:
            print(f"  closed by {reason:<9} {len(sel):>4} ({100 * len(sel) / len(trades):>4.1f}%)"
                  f"  {sum(t['oz'] for t in sel):+9.2f} $/oz")
    held = [(t["closed"] - t["opened"]).total_seconds() / 3600 for t in trades]
    print(f"  time in a trade     median {statistics.median(held):.1f}h, longest {max(held):.1f}h")

    balance = peak = worst = 0.0
    streak = longest = 0
    for t in trades:
        balance += t["oz"]
        peak = max(peak, balance)
        worst = max(worst, peak - balance)
        streak = 0 if t["oz"] > 0 else streak + 1
        longest = max(longest, streak)
    print(f"  worst drawdown      -{worst:.2f} $/oz  ({money(-worst)} at 0.01 lots)")
    print(f"  longest losing run  {longest} trades")

    months: OrderedDict = OrderedDict()
    for t in trades:
        m = months.setdefault(t["closed"].strftime("%Y-%m"), {"n": 0, "oz": 0.0})
        m["n"] += 1
        m["oz"] += t["oz"]
    red = sum(1 for m in months.values() if m["oz"] < 0)
    print(f"\n  month by month -- {red} of {len(months)} lost money")
    for name, m in months.items():
        print(f"    {name}   {m['n']:>4} trades  {m['oz']:+8.2f} $/oz")

    ok_total, ok_halves = total > 0, (mean(first) > 0 and mean(second) > 0)
    ok_n = len(trades) >= MIN_TRADES
    print("\n" + "=" * 92)
    print(f"THE BAR: profitable after costs, in BOTH halves, {MIN_TRADES}+ trades.")
    print(f"  after costs      {'PASS' if ok_total else 'FAIL'}   ({total:+.2f} $/oz)")
    print(f"  both halves      {'PASS' if ok_halves else 'FAIL'}   "
          f"({mean(first):+.3f} / {mean(second):+.3f})")
    print(f"  enough trades    {'PASS' if ok_n else 'FAIL'}   ({len(trades)})")
    if ok_total and ok_halves and ok_n:
        print("\n  VERDICT: PASSES -- and that is NOT a result yet. Testing several strategies")
        print("  means roughly one passes by luck. Next it must pass on a period it has never")
        print("  seen, and only then three weeks forward on demo.")
    else:
        print("\n  VERDICT: FAILS")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    until = (datetime.strptime(args.until, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
             if args.until else datetime.now(timezone.utc))

    config = load_config(validate_account_name(args.account))
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        df = get_ohlc_range(connector, config.symbol, args.timeframe,
                            since - timedelta(days=10), until, offset)
    finally:
        connector.disconnect()

    df = add_indicators(df)
    df = df[df.index >= since]
    if args.strategy == "orb":
        hh, mm = (int(x) for x in args.orb_session_start.split(":"))
        signals = signals_orb(df, time(hh, mm))
    else:
        signals = SIGNALS[args.strategy](df)
    trades = simulate(df, signals, args.strategy, timedelta(hours=args.max_hold_hours))

    report(trades, f"STRATEGY LAB -- {args.strategy.upper()}", [
        f"{config.symbol} {args.timeframe}   {since:%Y-%m-%d} to {until:%Y-%m-%d}   "
        f"{len(df):,} candles",
        f"costs {COSTS_PER_OZ:.2f} $/oz a trade   a candle touching both stop and target "
        f"counts as the STOP"
        + (f"   range hour {args.orb_session_start} UTC" if args.strategy == "orb" else ""),
    ])
    print("\nWhat this canNOT tell you: candles hide the order of moves inside one candle,")
    print("the spread is assumed constant, and a real fill is not guaranteed at the modelled")
    print("price in a fast market.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
