"""How much of the move is already gone when the cross confirms?

The one measure that has pointed the same way in every study here: the
monster losses are LATE entries. On demo2_m3 they had travelled 9.83
against 5.17 for every other trade; on demo2_m5 11.61 against 6.56; on
live2_m5 16.11 against 5.86 (2026-09-21 fingerprint).

THE COMPLICATION THIS TEST EXISTS TO PRICE. In the same fingerprint, the
most-travelled quartile ALSO earned the best average (+1.21 $/oz, 65%
won). Late entries are where the biggest wins and the biggest losses both
live -- so "skip the late ones" may simply cut the best trades, exactly
like the stop loss and the smaller targets did. Direction alone proves
nothing; only the money does.

MEASURED AT THE SIGNAL CANDLE -- the last candle CLOSED before the bot
entered, so nothing here needs hindsight:

  travelled   how far price moved in the trade's own direction over the
              last 10 candles (30 min on M3, 50 min on M5)
  gap         |close - EMA13|: how far price sits from the line it crossed
  stretch     |close - EMA21| in ATR(14)s: the same idea, volatility-scaled

Then it prices the actual rule: SKIP the entry when the measure is at or
above a threshold. Skipped trades score zero, everything else is left as
it really happened.

THE BAR, set before the first run: a threshold is worth building only if
it GAINS money in BOTH halves, on BOTH M3 accounts AND both M5 accounts.
A threshold that wins overall but loses in one half is noise -- that rule
has killed a dozen ideas here and saved the money each time.

    python scripts/late_entry_test.py --since "2026-08-25 00:00:00"
    python scripts/late_entry_test.py --offset-hours 3      (weekends)

Read-only. Entries are otherwise unchanged; a skipped trade would have
left the bot flat, so later reversal re-entries would differ -- that
alternative history is not simulated.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.indicators.range_filter import with_atr
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0
MONSTER = -8.0          # $/oz -- the worst ~10% of losses
LOOK_BACK_CANDLES = 10
QUINTILES = 5


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5,live2_m3,live2_m5,demo1_m3,demo1_m5")
    p.add_argument("--since", default="2026-08-25 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def buckets(rows: list, key: str, order: dict, half: int) -> None:
    vals = sorted(r[key] for r in rows)
    if len(vals) < QUINTILES * 4:
        print(f"    {key}: too few trades to split")
        return
    edges = [vals[int(len(vals) * i / QUINTILES)] for i in range(1, QUINTILES)]
    print(f"    {'band':<22} {'n':>4} {'won':>5} {'$/oz':>7} {'monsters':>9} "
          f"{'1st half':>9} {'2nd half':>9} {'money':>11}")
    for i in range(QUINTILES):
        lo = -1e9 if i == 0 else edges[i - 1]
        hi = 1e9 if i == QUINTILES - 1 else edges[i]
        group = [r for r in rows if lo <= r[key] < hi] if i < QUINTILES - 1 else \
                [r for r in rows if r[key] >= lo]
        if not group:
            continue
        w = sum(1 for r in group if r["oz"] > 0)
        mon = sum(1 for r in group if r["oz"] <= MONSTER)
        f = mean([r["oz"] for r in group if order[id(r)] < half])
        s = mean([r["oz"] for r in group if order[id(r)] >= half])
        label = (f"under {hi:.1f}" if i == 0 else
                 f"{lo:.1f} and up" if i == QUINTILES - 1 else f"{lo:.1f} to {hi:.1f}")
        print(f"    {label:<22} {len(group):>4} {100 * w / len(group):>4.0f}% "
              f"{mean([r['oz'] for r in group]):>+7.2f} {mon:>4} ({100 * mon / len(group):>2.0f}%) "
              f"{f:>+9.2f} {s:>+9.2f} {money(sum(r['profit'] for r in group)):>11}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("LATE ENTRIES -- how much of the move was already gone, and what it was worth")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  Outcome in $/oz, so lot size tilts nothing.")
    print("=" * 100)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(days=2), now, offset)
        finally:
            connector.disconnect()
        df = with_atr(compute_emas(df, config.ema_periods))

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            prior = df[df.index + timedelta(minutes=1) <= entry_utc]
            if len(prior) < LOOK_BACK_CANDLES + 2:
                continue
            signal = prior.iloc[-1]
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            atr = float(signal["atr"])
            rows.append({
                "entry": entry_utc,
                "profit": float(t["profit"]),
                "oz": float(t["profit"]) / (float(t["volume"]) * OZ_PER_LOT),
                "travelled": sign * (float(signal["close"])
                                     - float(prior["close"].iloc[-LOOK_BACK_CANDLES - 1])),
                "gap": abs(float(signal["close"]) - float(signal["ema13"])),
                "stretch": (abs(float(signal["close"]) - float(signal["ema21"])) / atr
                            if atr and atr == atr else float("nan")),
            })
        rows = [r for r in rows if r["stretch"] == r["stretch"]]

        print(f"\n{'=' * 100}\n{account}   {config.timeframe}   {len(rows)} trades   "
              f"overall {mean([r['oz'] for r in rows]):+.2f} $/oz\n{'=' * 100}")
        if len(rows) < QUINTILES * 4:
            print("  too few trades here to split; shown for completeness")
            continue
        order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry"]))}
        half = len(rows) // 2

        for key, title in (("travelled", "how far price had already run our way (10 candles)"),
                           ("gap", "distance from EMA13 at the cross"),
                           ("stretch", "distance from EMA21, in ATRs")):
            print(f"\n  {title}")
            buckets(rows, key, order, half)

        print("\n  THE RULE: skip the entry when 'travelled' is at or above")
        real = sum(r["profit"] for r in rows)
        vals = sorted(r["travelled"] for r in rows)
        grid = [vals[int(len(vals) * p)] for p in (0.60, 0.70, 0.80, 0.90, 0.95)]
        print(f"    {'threshold':>10} {'skipped':>8} {'net':>12} {'vs real':>11} "
              f"{'1st half':>10} {'2nd half':>10}")
        for cut in grid:
            skipped = [r for r in rows if r["travelled"] >= cut]
            kept = [r for r in rows if r["travelled"] < cut]
            d1 = -sum(r["profit"] for r in skipped if order[id(r)] < half)
            d2 = -sum(r["profit"] for r in skipped if order[id(r)] >= half)
            net = sum(r["profit"] for r in kept)
            print(f"    {cut:>10.2f} {len(skipped):>8} {money(net):>12} {money(net - real):>11} "
                  f"{money(d1):>10} {money(d2):>10}")

    print(f"\n{'=' * 100}")
    print("THE BAR (set before this run): a threshold is worth building only if it GAINS in BOTH")
    print("halves, on BOTH M3 accounts AND both M5 accounts. Skipping the most-travelled entries")
    print("also removes the biggest WINS -- that is what these numbers are here to weigh.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
