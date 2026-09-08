"""Does the EMA13/21 cross get better on longer timeframes?

User's question 2026-09-08: *"instead of one minute replace the 15min
timeframe, can you tell me your idea how is that?"*

WHAT WE ALREADY KNOW. The naked cross -- enter on a confirmed cross,
exit on the opposite one, no TP/stop/runner -- loses on M1 in every cut
and earns +$0.451/trade on M3. Two points make a line, and the line
says "longer is better". Two points also fit an arch, which would say
"M3 is the peak". Those predict opposite things about M15 and we cannot
tell them apart without measuring, so this measures.

WHY LONGER *SHOULD* HELP: EMA13/21 on M1 spans 13 and 21 minutes, so a
few dollars of noise flips the cross. On M15 the same EMAs span 3.25
and 5.25 HOURS -- a cross means the trend actually turned.

WHY LONGER *SHOULD* HURT: the engine only acts at candle close, so on
M15 it waits up to 15 minutes to confirm a move that has already run.
Right signal, late entry. This script measures that cost directly
(MISSED column) instead of assuming it.

PRE-REGISTERED, WRITTEN BEFORE THE FIRST RUN. Six timeframes are tested,
so a couple will look good by luck alone -- that is exactly how the
Efficiency-Ratio and colour-filter findings were retracted. A timeframe
is worth building only if it passes ALL FOUR:

  1. Positive expectancy in the FIRST half and the SECOND half separately
     (not just pooled).
  2. At least 100 trades in the window -- below that the average is noise.
  3. Beats M3's dollars-per-DAY, not per trade. M15 trading 3x a day at
     +$2 loses to M3 trading 30x at +$0.45, and per-trade figures hide it.
  4. Still positive when the cost per trade is doubled (--spread 0.24),
     since a rule that only works at the exact assumed cost is not robust.

Anything failing one of these is a "no", however good the headline looks.

ALSO REPORTED, because the current $5-$7 dollar levels were fitted to M1
and M3 candles and cannot simply be carried to M15:
  - median candle RANGE per timeframe: the noise floor a stop must clear.
    A $5 stop inside a $6 candle range is hit by ordinary noise before
    the signal can work.
  - whether that range really scales as sqrt(time), which is the
    assumption behind "M15 needs ~2.2x M3's levels" (from
    project_post_exit_movement_null). Checked here, not trusted.
  - the move distribution, so a take-profit can be placed from data.

    python scripts/timeframe_expectancy.py --days 180
    python scripts/timeframe_expectancy.py --days 180 --spread 0.24   # criterion 4

Read-only: fetches candles, places nothing.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

USD_PER_LOT_PER_DOLLAR = 100.0
TIMEFRAMES = ["M1", "M3", "M5", "M15", "M30", "H1"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo1_m3", help="only for symbol + credentials")
    p.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--spread", type=float, default=0.12, help="cost per trade in price dollars")
    p.add_argument("--lots", type=float, default=0.12)
    return p.parse_args()


def cross_trades(df: pd.DataFrame) -> list[dict]:
    """Cross to opposite cross, entering at the signal candle's close --
    the same state-CHANGE test the live engines use (see
    bot/strategy/cross_lookup.py, which fixed the wrong-candle bug)."""
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False          # row 0 has no predecessor
    crosses = df.index[changed]

    trades = []
    for i in range(len(crosses) - 1):
        entry_t, exit_t = crosses[i], crosses[i + 1]
        is_buy = bool(above.loc[entry_t])
        entry = float(df.loc[entry_t, "close"])
        exit_ = float(df.loc[exit_t, "close"])
        sig_open = float(df.loc[entry_t, "open"])
        window = df.loc[entry_t:exit_t]
        # Best and worst the trade ever saw, which is what a TP or stop
        # would actually have caught -- close-to-close hides both.
        if is_buy:
            move = exit_ - entry
            best, worst = float(window["high"].max()) - entry, float(window["low"].min()) - entry
        else:
            move = entry - exit_
            best, worst = entry - float(window["low"].min()), entry - float(window["high"].max())
        trades.append({
            "entry_time": entry_t,
            "move": move,
            "best": best,
            "worst": worst,
            # What waiting for the candle to close gave up: the signal
            # candle's own body, in the trade's direction.
            "missed": (entry - sig_open) if is_buy else (sig_open - entry),
        })
    return trades


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    config = load_config(validate_account_name(args.account))
    timeframes = [t.strip().upper() for t in args.timeframes.split(",")]
    to_usd = args.lots * USD_PER_LOT_PER_DOLLAR

    print("=" * 100)
    print(f"TIMEFRAME EXPECTANCY — naked EMA13/21 cross on {config.symbol}, "
          f"{args.days} days, ${args.spread:.2f}/trade cost, {args.lots} lots")
    print("=" * 100)
    print("PRE-REGISTERED (fixed before this ran): a timeframe passes only if it is positive in")
    print("BOTH halves, has >=100 trades, beats M3 per DAY, and survives doubled cost.")
    print()

    rows = []
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        for tf in timeframes:
            df = get_ohlc_range(connector, config.symbol, tf, since, now)
            if df is None or len(df) < 200:
                print(f"  {tf}: only {0 if df is None else len(df)} candles — skipped.")
                continue
            df = compute_emas(df, config.ema_periods)
            trades = cross_trades(df)
            if len(trades) < 10:
                print(f"  {tf}: only {len(trades)} trades — skipped.")
                continue

            net = [t["move"] - args.spread for t in trades]
            mid = len(net) // 2
            first, second = net[:mid], net[mid:]
            span_days = max((df.index[-1] - df.index[0]).total_seconds() / 86400.0, 1.0)

            equity, peak, mdd = 0.0, 0.0, 0.0
            for m in net:
                equity += m
                peak = max(peak, equity)
                mdd = max(mdd, peak - equity)

            rows.append({
                "tf": tf,
                "candles": len(df),
                "n": len(trades),
                "win": 100 * sum(1 for m in net if m > 0) / len(net),
                "exp": sum(net) / len(net),
                "per_day": sum(net) / span_days,
                "total": sum(net),
                "mdd": mdd,
                "first": sum(first) / len(first) if first else 0.0,
                "second": sum(second) / len(second) if second else 0.0,
                "range": float((df["high"] - df["low"]).median()),
                "missed": pd.Series([t["missed"] for t in trades]).median(),
                "best50": pct([t["best"] for t in trades], 0.50),
                "best75": pct([t["best"] for t in trades], 0.75),
                "worst50": pct([abs(t["worst"]) for t in trades], 0.50),
            })
    finally:
        connector.disconnect()

    if not rows:
        print("No timeframe produced usable data.")
        return

    m3 = next((r for r in rows if r["tf"] == "M3"), None)

    print(f"{'TF':<5}{'trades':>8}{'win%':>7}{'$/trade':>10}{'$/DAY':>10}{'total$':>11}"
          f"{'maxDD$':>10}{'ret/DD':>8}{'1st half':>10}{'2nd half':>10}  verdict")
    print("-" * 100)
    for r in rows:
        both = r["first"] > 0 and r["second"] > 0
        enough = r["n"] >= 100
        # M3 is the bar, so it cannot be asked to beat itself.
        beats = m3 is None or r["tf"] == "M3" or r["per_day"] > m3["per_day"]
        if not both:
            verdict = "NO — fails walk-forward"
        elif not enough:
            verdict = f"NO — only {r['n']} trades"
        elif not beats:
            verdict = "NO — earns less/day than M3"
        else:
            verdict = "PASSES — re-run with --spread 0.24"
        if r["tf"] == "M3":
            verdict = "the incumbent — this is the bar"
        print(f"{r['tf']:<5}{r['n']:>8}{r['win']:>6.1f}%{r['exp'] * to_usd:>10.2f}"
              f"{r['per_day'] * to_usd:>10.2f}{r['total'] * to_usd:>11.0f}{r['mdd'] * to_usd:>10.0f}"
              f"{(r['total'] / r['mdd'] if r['mdd'] else 0):>8.2f}"
              f"{r['first'] * to_usd:>10.2f}{r['second'] * to_usd:>10.2f}  {verdict}")

    # ---- can the current dollar levels even be carried over? ----------
    print()
    print("=" * 100)
    print("CANDLE SIZE — why $5/$7 cannot simply move to a longer timeframe")
    print("=" * 100)
    print(f"{'TF':<5}{'median':>9}{'sqrt(t)':>10}{'MISSED':>9}{'  median':>10}{'  75th':>9}"
          f"{'  median':>10}   suggested from data")
    print(f"{'':<5}{'range':>9}{'predicts':>10}{'by close':>9}{'  best':>10}{'  best':>9}"
          f"{'  adverse':>10}   TP / stop")
    print("-" * 100)
    base = next((r for r in rows if r["tf"] == "M3"), rows[0])
    for r in rows:
        scale = math.sqrt(minutes_for(r["tf"]) / minutes_for(base["tf"]))
        print(f"{r['tf']:<5}{r['range']:>9.2f}{base['range'] * scale:>10.2f}{r['missed']:>9.2f}"
              f"{r['best50']:>10.2f}{r['best75']:>9.2f}{r['worst50']:>10.2f}"
              f"   ${r['best50']:.2f} / ${max(r['worst50'], 2 * r['range']):.2f}")
    print()
    print("median range  = the noise floor. A stop below it is hit by ordinary movement.")
    print("sqrt(t)       = what M3's range scales to. If the two columns disagree, the")
    print("                '2.2x M3' rule of thumb is wrong and levels must come from data.")
    print("MISSED        = median dollars already gone by the time the candle closed and the")
    print("                engine could act. This is the price of a longer timeframe.")
    print("median best   = the typical favourable excursion, i.e. where a take-profit belongs.")
    print()
    print("A PASS here is permission to research further, not to deploy. Any new timeframe")
    print("goes on demo beside the existing legs with scaled lots -- never replacing one.")


if __name__ == "__main__":
    main()
