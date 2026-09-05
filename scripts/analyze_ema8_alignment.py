"""Does adding EMA8 tell us anything the EMA13/21 cross doesn't?

User's question (2026-09-05): if we also watched EMA8 alongside the
existing EMA13/21 pair, could we have identified entry confirmation
earlier or better on our past trades?

Structurally different from every entry filter tested so far -- those
all measured CANDLE properties (colour, volume, body size, ATR). This
measures the RELATIONSHIP BETWEEN THE EMA LINES, which is untested
ground in this project.

Two questions, answered separately:

EARLY WARNING -- EMA8 is faster than EMA13, so it crosses EMA21 first.
    How many candles ahead? If it is consistently several candles early,
    it could serve as a heads-up. (Note: acting on it directly means
    entering before confirmation, which this project already tested and
    found produced 83% of all losses -- see
    project_dual_cross_and_cross_confirmed. So the value here would be
    as a filter, not a trigger.)

CONFIRMATION QUALITY -- at the moment EMA13 crosses EMA21, where is
    EMA8? Three states:
      ALIGNED    ema8 > ema13 > ema21 (BUY) -- fast line leading, clean
      PARTIAL    ema8 above ema21 but not above ema13 -- mixed
      AGAINST    ema8 below ema21 -- fast line already rolled over, the
                 move may be fading even as the slow cross confirms
    Split real outcomes by these states, per account, walk-forward.

Cross detection uses the genuine state-change test
(`above != above.shift(1)`) -- NOT the "EMAs are on the right side"
search that produced a real bug on 2026-09-04 (see
project_trade_protection_findings).

    python scripts/analyze_ema8_alignment.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

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
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

FAST_SPAN = 8


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def state_changes(above: pd.Series) -> pd.Series:
    changed = above != above.shift(1)
    changed.iloc[0] = False
    return changed


def find_cross_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    """Candle where EMA13/21 genuinely CHANGED state into this direction."""
    above = df["ema13"] > df["ema21"]
    changed = state_changes(above)
    want = direction == "BUY"
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        if changed.loc[idx] and bool(above.loc[idx]) == want:
            return idx
    return None


def summarize(label: str, profits: list[float]) -> str:
    if not profits:
        return f"    {label:<34} n=0"
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    total = sum(profits)
    return (f"    {label:<34} n={n:<4} {100*wins/n:5.1f}% win  "
            f"total ${total:+9.2f}  avg ${total/n:+7.2f}/trade")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=10), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        df["ema8"] = df["close"].ewm(span=FAST_SPAN, adjust=False).mean()

        # every genuine EMA8/EMA21 state change, for the early-warning question
        fast_above = df["ema8"] > df["ema21"]
        fast_changed = state_changes(fast_above)

        buckets: dict[str, list[float]] = {"ALIGNED": [], "PARTIAL": [], "AGAINST": []}
        rows = []
        leads = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_cross_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            r = df.loc[ct]
            e8, e13, e21 = float(r["ema8"]), float(r["ema13"]), float(r["ema21"])
            is_buy = t["direction"] == "BUY"
            if is_buy:
                state = "ALIGNED" if e8 > e13 > e21 else ("PARTIAL" if e8 > e21 else "AGAINST")
            else:
                state = "ALIGNED" if e8 < e13 < e21 else ("PARTIAL" if e8 < e21 else "AGAINST")
            buckets[state].append(t["profit"])
            rows.append({"time": entry_utc, "profit": t["profit"], "state": state})

            # how many candles earlier did EMA8 cross EMA21 the same way?
            pos = df.index.get_loc(ct)
            prior = df.index[:pos + 1]
            want = is_buy
            lead = None
            for i in range(pos, max(-1, pos - 200), -1):
                idx = df.index[i]
                if fast_changed.loc[idx] and bool(fast_above.loc[idx]) == want:
                    lead = pos - i
                    break
            if lead is not None:
                leads.append(lead)

        if not rows:
            print(f"{account}: no matched trades.\n")
            continue
        rows.sort(key=lambda r: r["time"])

        n = len(rows)
        print(f"{'=' * 78}\n{account}: {n} trades matched to a genuine EMA13/21 cross\n{'=' * 78}")

        print("  EMA8 POSITION AT THE MOMENT OF THE EMA13/21 CROSS:")
        for state in ("ALIGNED", "PARTIAL", "AGAINST"):
            print(summarize(state, buckets[state]))

        # walk-forward on the aligned-vs-not split
        mid = n // 2
        for half_label, half in (("first half ", rows[:mid]), ("second half", rows[mid:])):
            al = [r["profit"] for r in half if r["state"] == "ALIGNED"]
            no = [r["profit"] for r in half if r["state"] != "ALIGNED"]
            a_avg = sum(al) / len(al) if al else 0.0
            n_avg = sum(no) / len(no) if no else 0.0
            print(f"      {half_label}: aligned avg ${a_avg:+7.2f} (n={len(al)})  vs  "
                  f"not aligned avg ${n_avg:+7.2f} (n={len(no)})")

        if leads:
            print(f"\n  EARLY WARNING: EMA8 crossed EMA21 a median of "
                  f"{statistics.median(leads):.0f} candles before the EMA13/21 cross")
            print(f"      (mean {statistics.mean(leads):.1f}, range {min(leads)}-{max(leads)}, "
                  f"{len(leads)}/{n} trades had a prior EMA8 cross)")
        print()


if __name__ == "__main__":
    main()
