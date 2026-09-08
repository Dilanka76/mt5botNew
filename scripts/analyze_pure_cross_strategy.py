"""The naked strategy: enter on a confirmed cross, exit only on the
OPPOSITE confirmed cross. No take-profit, no stop-loss, no breakeven, no
runner, no swap gate. Always in the market, flipping at every cross.

User's question 2026-09-08: *"think we are not any runner and any tp, so
only cross confirmed enter and then only opposite cross confirmed exit
then enter new trade -- how is the accuracy to win or loss, and is this
best or worst?"*

WHY IT IS WORTH KNOWING. Every rule in this project sits on top of the
EMA13/21 cross, and almost all of the effort has gone into the exits:
take-profit, stop, breakeven, swap, and now the TP-runner. Nobody has
ever measured what the SIGNAL earns on its own. That answers a question
the tuning cannot:

  - If the naked signal is PROFITABLE, the edge is in the cross and the
    exit rules are there to shape the ride. Worth building on.
  - If it LOSES, the cross is close to a coin flip and every dollar the
    strategy makes comes from exit management. That is a very different
    thing to be running, and it would explain why about a dozen ENTRY
    filters all failed while the exit changes are the only ones that
    have ever helped.

METHOD -- deliberately simple, so it is easy to check:
  1. A confirmed cross is a genuine EMA13/21 state CHANGE at candle
     close (`above != above.shift(1)`), the same test the live engines
     use and the same one bot/strategy/cross_lookup.py fixed.
  2. Enter at that candle's close. Exit at the next opposite cross's
     close. Repeat. There is no other exit.
  3. Charge the spread once per trade (--spread, default $0.12 as
     measured on XAUUSDp). Nothing else: no slippage, no commission, no
     swap. So the result is OPTIMISTIC and should be read as a ceiling.

Reports per timeframe, because everything in this project splits that
way (see feedback_m1_m3_candle_behaviour): win rate, average win and
loss, expectancy per trade, the biggest moves, and how it compares with
the account's real take-profit and stop.

    python scripts/analyze_pure_cross_strategy.py --accounts demo1_m1,demo1_m3 --days 180

Read-only: fetches candles, places nothing.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

USD_PER_LOT_PER_DOLLAR = 100.0
COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    p.add_argument("--days", type=int, default=180, help="how far back to test (default 180)")
    p.add_argument("--spread", type=float, default=0.12, help="cost per trade in price dollars")
    p.add_argument("--lots", type=float, default=0.12, help="fixed lot size for the $ column")
    return p.parse_args()


def cross_trades(df: pd.DataFrame) -> list[dict]:
    """Every cross-to-cross trade. Enter at the cross candle's close,
    exit at the next opposite cross's close."""
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
        move = (exit_ - entry) if is_buy else (entry - exit_)
        trades.append({
            "entry_time": entry_t, "exit_time": exit_t,
            "direction": "BUY" if is_buy else "SELL",
            "move": move,
            "candles": df.index.get_loc(exit_t) - df.index.get_loc(entry_t),
        })
    return trades


def report(label: str, trades: list[dict], spread: float, lots: float, config) -> None:
    if not trades:
        print(f"  {label}: no trades.")
        return
    net = [t["move"] - spread for t in trades]
    wins = [m for m in net if m > 0]
    losses = [m for m in net if m <= 0]
    total = sum(net)
    to_usd = lots * USD_PER_LOT_PER_DOLLAR

    print(f"  {label}")
    print(f"    trades           : {len(trades)}")
    print(f"    win rate         : {100 * len(wins) / len(trades):.1f}%")
    print(f"    avg win          : ${sum(wins) / len(wins) if wins else 0:+.2f} of price "
          f"(${(sum(wins) / len(wins) if wins else 0) * to_usd:+.2f} at {lots} lots)")
    print(f"    avg loss         : ${sum(losses) / len(losses) if losses else 0:+.2f} of price "
          f"(${(sum(losses) / len(losses) if losses else 0) * to_usd:+.2f})")
    print(f"    expectancy/trade : ${total / len(trades):+.3f} of price "
          f"(${(total / len(trades)) * to_usd:+.2f})")
    print(f"    TOTAL            : ${total:+.2f} of price (${total * to_usd:+.2f})")
    moves = sorted(t["move"] for t in trades)
    print(f"    biggest win/loss : ${moves[-1]:+.2f} / ${moves[0]:+.2f} of price")
    print(f"    median hold      : {sorted(t['candles'] for t in trades)[len(trades) // 2]} candles")

    # How the account's real levels compare with what the raw signal offers.
    tp, sl = config.take_profit_usd, config.stop_loss_usd
    reached_tp = sum(1 for t in trades if t["move"] >= tp)
    beyond_sl = sum(1 for t in trades if t["move"] <= -sl) if sl else 0
    print(f"    vs this account's real levels (TP ${tp:.2f}, stop ${sl:.2f}):")
    print(f"      {reached_tp} of {len(trades)} ({100 * reached_tp / len(trades):.0f}%) moved far "
          f"enough to hit the take-profit")
    if sl:
        print(f"      {beyond_sl} of {len(trades)} ({100 * beyond_sl / len(trades):.0f}%) went far "
              f"enough against to hit the stop")
    print()



def by_hour(trades: list[dict], spread: float, lots: float) -> None:
    """Expectancy per entry hour, with the walk-forward split INSIDE each
    hour. An hour only counts if it is positive in BOTH halves -- a whole
    day has 24 buckets, so a few will look good by chance alone, and
    ranking by the pooled figure would just surface the luckiest.

    This is the one lever left for M1: its signal misses break-even by
    about one percentage point of win rate, so a subset of the day that
    is genuinely better could carry it over. See
    feedback_m1_m3_candle_behaviour.
    """
    mid = len(trades) // 2
    first_half = set(id(t) for t in trades[:mid])
    buckets: dict[int, list[dict]] = {}
    for t in trades:
        buckets.setdefault(t["entry_time"].astimezone(COLOMBO).hour, []).append(t)

    to_usd = lots * USD_PER_LOT_PER_DOLLAR
    print(f"  BY ENTRY HOUR (Colombo) — an hour must be positive in BOTH halves to count")
    print(f"    {'hour':<8}{'n':>6}{'win%':>8}{'$/trade':>10}{'1st half':>11}{'2nd half':>11}  verdict")
    keepers = []
    for hour in sorted(buckets):
        rows = buckets[hour]
        net = [t["move"] - spread for t in rows]
        exp = sum(net) / len(net)
        wins = sum(1 for m in net if m > 0)
        a = [t["move"] - spread for t in rows if id(t) in first_half]
        b = [t["move"] - spread for t in rows if id(t) not in first_half]
        exp_a = sum(a) / len(a) if a else 0.0
        exp_b = sum(b) / len(b) if b else 0.0
        both = exp_a > 0 and exp_b > 0 and len(a) >= 20 and len(b) >= 20
        if both:
            keepers.append(hour)
        print(f"    {hour:02d}:00   {len(rows):>6}{100 * wins / len(rows):>7.1f}%"
              f"{exp * to_usd:>10.2f}{exp_a * to_usd:>11.2f}{exp_b * to_usd:>11.2f}"
              f"  {'KEEP' if both else ''}")

    if keepers:
        kept = [t for t in trades if t["entry_time"].astimezone(COLOMBO).hour in keepers]
        net = [t["move"] - spread for t in kept]
        print(f"\n    Hours positive in both halves: "
              f"{', '.join(f'{h:02d}:00' for h in keepers)}")
        print(f"    Trading ONLY those hours: {len(kept)} of {len(trades)} trades, "
              f"${sum(net) / len(net) * to_usd:+.2f}/trade, total ${sum(net) * to_usd:+.2f}")
        print(f"    (vs all hours: ${sum(t['move'] - spread for t in trades) / len(trades) * to_usd:+.2f}"
              f"/trade, total ${sum(t['move'] - spread for t in trades) * to_usd:+.2f})")
    else:
        print("\n    NO hour is positive in both halves. There is no time-of-day subset")
        print("    that rescues this signal -- the weakness is spread across the whole day.")
    print()


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since, now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        trades = cross_trades(df)
        print("=" * 78)
        print(f"{account} ({config.timeframe}) — naked EMA13/21 cross, {args.days} days, "
              f"{len(df)} candles")
        print(f"spread charged: ${args.spread:.2f}/trade   lots: {args.lots}")
        print("=" * 78)
        report("Every trade", trades, args.spread, args.lots, config)

        mid = len(trades) // 2
        report("First half (walk-forward)", trades[:mid], args.spread, args.lots, config)
        report("Second half", trades[mid:], args.spread, args.lots, config)

        by_hour(trades, args.spread, args.lots)

        for d in ("BUY", "SELL"):
            report(f"{d} only", [t for t in trades if t["direction"] == d],
                   args.spread, args.lots, config)

    print("Optimistic by construction: candle closes only, no slippage beyond one spread,")
    print("no commission, no swap, and it assumes you are ALWAYS in the market. Read the")
    print("result as a ceiling on what the raw signal can give.")


if __name__ == "__main__":
    main()
