"""Two questions in one pass, both needed before Efficiency Ratio could
become a live rule.

QUESTION 1 -- is the ER effect real, measured in a way the losing-period
artifact cannot fake?

scripts/analyze_efficiency_ratio.py showed a clear relationship on the
M3 accounts (higher ER = better outcomes) but its walk-forward test
failed on 31 of 32 combinations. Reading the output explains why: nearly
every combination is negative in the first half and positive in the
second, on every account and every threshold. The second half of this
data was a losing period, and in a losing period ANY filter that removes
trades looks good -- so a "P/L with the rule vs without" comparison is
dominated by the period, not the rule, and would show that shape for a
random filter too.

The fix is to compare the AVERAGE PER TRADE of high-ER versus low-ER
trades WITHIN each half separately. That is immune to how profitable the
half was overall -- it asks only "did high-ER trades beat low-ER trades
here?", which is the actual question. Same reasoning as the forward
shadow check in scripts/check_shadow_filter_forward_results.py.

QUESTION 2 -- does ER tell us anything ADX doesn't already?

demo1's engine already computes ADX(14) and uses it to gate swaps. Both
ADX and ER claim to measure trend strength versus chop. If they move
together, ER is a second name for something demo1 already has. If they
disagree, they are seeing different things. Reports the correlation and
a cross-tab of high/low ADX against high/low ER.

    python scripts/analyze_er_vs_adx.py --since "2026-08-25 00:00:00" --offset-hours 3

Read-only: connects to MT5 only to read historical data.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.adx import compute_adx
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

ADX_THRESHOLD = 25.0  # the value demo1's swap gate actually uses


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo2_m1,demo1_m3,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker offset, supplied deliberately when the market is closed (this broker: 3)")
    return p.parse_args()


def efficiency_ratios(df: pd.DataFrame, lookback: int) -> tuple[pd.Series, pd.Series]:
    close = df["close"]
    net = (close - close.shift(lookback)).abs()
    total_close = close.diff().abs().rolling(lookback).sum()
    prev_close = close.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return net / total_close, net / tr.rolling(lookback).sum()


def find_cross_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    want_above = direction == "BUY"
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        if changed.loc[idx] and bool(above.loc[idx]) == want_above:
            return idx
    return None


def avg(rows: list[dict]) -> float:
    return sum(r["profit"] for r in rows) / len(rows) if rows else 0.0


def split_test(label: str, rows: list[dict], key: str, cut: float) -> None:
    """Per-trade averages of high vs low, in each half separately.
    Immune to how profitable the half was overall."""
    mid = len(rows) // 2
    verdicts = []
    print(f"    {label} (split at {cut:.2f})")
    for half_name, half in (("full   ", rows), ("1st half", rows[:mid]), ("2nd half", rows[mid:])):
        hi = [r for r in half if r[key] >= cut]
        lo = [r for r in half if r[key] < cut]
        if not hi or not lo:
            print(f"      {half_name}: not enough trades on both sides")
            verdicts.append(None)
            continue
        gap = avg(hi) - avg(lo)
        verdicts.append(gap)
        print(f"      {half_name}: high ER n={len(hi):<3} ${avg(hi):+7.2f}/trade   "
              f"low ER n={len(lo):<3} ${avg(lo):+7.2f}/trade   gap ${gap:+7.2f}")
    h1, h2 = verdicts[1], verdicts[2]
    if h1 is not None and h2 is not None:
        if h1 > 0 and h2 > 0:
            print(f"      -> HOLDS: high ER beat low ER in BOTH halves")
        elif h1 < 0 and h2 < 0:
            print(f"      -> REVERSED: low ER beat high ER in both halves")
        else:
            print(f"      -> SPLIT: disagrees between halves")
    print()


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    print(f"ER lookback {args.lookback} candles; ADX(14) threshold {ADX_THRESHOLD}")
    if args.offset_hours is not None:
        print(f"NOTE: broker offset supplied manually as +{args.offset_hours}h (market closed)")
    print()

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(days=5), now, offset=offset)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        df = compute_adx(df)                      # same Wilder ADX demo1's swap gate uses
        er_close, er_tr = efficiency_ratios(df, args.lookback)

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_cross_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            a, b, adx = er_close.get(ct), er_tr.get(ct), df["adx"].get(ct)
            if any(v is None or pd.isna(v) for v in (a, b, adx)):
                continue
            rows.append({"time": entry_utc, "profit": t["profit"],
                         "close_er": float(a), "tr_er": float(b), "adx": float(adx)})
        if not rows:
            print(f"{account}: no matched trades.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        total = sum(r["profit"] for r in rows)
        print(f"{'=' * 84}\n{account} ({config.timeframe}): {len(rows)} trades, total ${total:+.2f}\n{'=' * 84}")

        print("  QUESTION 1 -- does high ER beat low ER within each half? (artifact-immune)")
        for key, cut in (("close_er", 0.10), ("tr_er", 0.05), ("tr_er", 0.10)):
            name = "close-only ER" if key == "close_er" else "true-range ER"
            split_test(name, rows, key, cut)

        print("  QUESTION 2 -- does ER tell us anything ADX doesn't?")
        er_vals = [r["close_er"] for r in rows]
        tr_vals = [r["tr_er"] for r in rows]
        adx_vals = [r["adx"] for r in rows]
        try:
            c1 = statistics.correlation(er_vals, adx_vals)
            c2 = statistics.correlation(tr_vals, adx_vals)
            print(f"    correlation with ADX:  close-only ER {c1:+.2f}   true-range ER {c2:+.2f}")
            print(f"    (near +1 = same signal, near 0 = independent)")
        except statistics.StatisticsError:
            print("    correlation unavailable (no variance)")

        print(f"    ADX median {statistics.median(adx_vals):.1f}; "
              f"{sum(1 for v in adx_vals if v >= ADX_THRESHOLD)}/{len(adx_vals)} trades had ADX >= {ADX_THRESHOLD}")
        print(f"    {'':>18}{'ADX < 25':<26}ADX >= 25")
        for er_label, er_hi in (("ER low  (<0.10)", False), ("ER high (>=0.10)", True)):
            cells = []
            for adx_hi in (False, True):
                sel = [r for r in rows
                       if ((r["close_er"] >= 0.10) == er_hi) and ((r["adx"] >= ADX_THRESHOLD) == adx_hi)]
                cells.append(f"n={len(sel):<4} ${avg(sel):+7.2f}/trade" if sel else "n=0")
            print(f"    {er_label:<18}{cells[0]:<26}{cells[1]}")
        print()


if __name__ == "__main__":
    main()
