"""The last entry test: is an M3 cross inside the H1 "parent candle" a trap?

User, 2026-09-21, from an outside write-up: M3/M5 EMA crosses fail while
all the movement sits inside the previous H1 candle's range ("the parent
candle trap"), and when the EMAs are squeezed flat. Agreed as the LAST
entry test -- about 17 entry measures have been tried on this data and
the pattern in the failures suggests the cross already takes what is
there. Every further look also raises the odds of a lucky-looking
pattern, so this one is deliberately narrow.

M3 ONLY. That is where this morning's data hinted at a squeeze effect
(tightest EMA13-21 spread was the worst quartile on demo2_m3 AND
live2_m3; both M5 accounts said the opposite).

TWO MEASURES, both knowable at the moment the bot decided:

  TRAPPED   every closed M3 candle so far this hour has stayed inside the
            previous complete H1 candle's high and low -- the market has
            not left the parent candle. When the entry is in the hour's
            first M3 candle, the entry price inside that range decides it.
  SQUEEZED  the EMA13-21 gap on the signal candle is in this account's
            own lowest quarter. A neutral cut, chosen before any outcome
            is looked at.

HONEST CAVEAT, printed with every run: the squeeze half was NOTICED on
this same data this morning, so this data cannot confirm it on its own.
The H1 trap is the genuinely new question. Forward trades are what can
settle either.

THE RULE, WRITTEN BEFORE THE RESULT (2026-09-21): the TRAP group --
trapped AND squeezed -- counts only if it LOSES money, in $/oz, in BOTH
halves, on BOTH demo2_m3 AND live2_m3. "Makes less but still makes
money" means those trades keep being taken.

    python scripts/h1_trap_test.py
    python scripts/h1_trap_test.py --offset-hours 3      (weekends)

Read-only.
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
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

OZ_PER_LOT = 100.0
MIN_GROUP = 10          # fewer trades than this in a group is not a result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,live2_m3")
    p.add_argument("--since", default="2026-08-25 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    p.add_argument("--squeeze-cut", type=float, default=None,
                   help="a FIXED EMA13-21 gap, in $, below which a cross counts as squeezed. "
                        "For the forward test: 0.042, frozen 2026-09-21 from demo2_m3's own "
                        "lowest quarter. Without it the cut is re-derived from the data, "
                        "which is only fair on the data it was first noticed on.")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def mean(xs: list) -> float:
    return statistics.mean(xs) if xs else float("nan")


def trapped_at(m3, h1, entry_utc: datetime, entry_price: float, bar: timedelta):
    """True/False, or None when the parent candle is not in the data."""
    hour_start = entry_utc.replace(minute=0, second=0, microsecond=0)
    parent_open = hour_start - timedelta(hours=1)
    if parent_open not in h1.index:
        return None
    parent = h1.loc[parent_open]
    top, bottom = float(parent["high"]), float(parent["low"])
    so_far = m3[(m3.index >= hour_start) & (m3.index + bar <= entry_utc)]
    if so_far.empty:
        return bottom <= entry_price <= top
    return float(so_far["high"].max()) <= top and float(so_far["low"].min()) >= bottom


def report(label: str, rows: list, order: dict, half: int) -> tuple:
    """Prints one group; returns (n, $/oz first half, $/oz second half)."""
    if not rows:
        print(f"    {label:<34} none")
        return 0, float("nan"), float("nan")
    w = sum(1 for r in rows if r["oz"] > 0)
    first = [r["oz"] for r in rows if order[id(r)] < half]
    second = [r["oz"] for r in rows if order[id(r)] >= half]
    f, s = mean(first), mean(second)
    print(f"    {label:<34} {len(rows):>4} trades  {100 * w / len(rows):>4.0f}% won  "
          f"{mean([r['oz'] for r in rows]):>+6.2f} $/oz   "
          f"halves {f:>+6.2f} / {s:>+6.2f}   {money(sum(r['profit'] for r in rows)):>11}")
    return len(rows), f, s


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 108)
    print("THE H1 PARENT-CANDLE TRAP -- M3 crosses inside the last H1 candle, with the EMAs squeezed")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  Outcome in $/oz, so lot size tilts nothing.")
    print("=" * 108)

    verdicts = {}
    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        if config.timeframe != "M3":
            print(f"\n{account}: {config.timeframe}, not M3 -- this test is M3 only. Skipped.")
            continue
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            m3 = get_ohlc_range(connector, config.symbol, "M3",
                                since - timedelta(days=3), now, offset)
            h1 = get_ohlc_range(connector, config.symbol, "H1",
                                since - timedelta(days=3), now, offset)
        finally:
            connector.disconnect()

        m3 = compute_emas(m3, config.ema_periods)
        bar = timedelta(minutes=minutes_for("M3"))

        rows = []
        unknown = 0
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            prior = m3[m3.index + bar <= entry_utc]
            if prior.empty:
                continue
            signal = prior.iloc[-1]
            trapped = trapped_at(m3, h1, entry_utc, float(t["entry_price"]), bar)
            if trapped is None:
                unknown += 1
                continue
            rows.append({
                "entry_utc": entry_utc,
                "profit": float(t["profit"]),
                "oz": float(t["profit"]) / (float(t["volume"]) * OZ_PER_LOT),
                "sep": abs(float(signal["ema13"]) - float(signal["ema21"])),
                "trapped": trapped,
            })

        print(f"\n{'=' * 108}")
        print(f"{account}   {len(rows)} trades" + (f"   ({unknown} with no parent H1 candle, left out)"
                                                    if unknown else ""))
        print("=" * 108)
        if len(rows) < 4 * MIN_GROUP:
            print(f"  fewer than {4 * MIN_GROUP} trades -- the four groups would be too small to mean anything.")

        # the lowest quarter of THIS account's own spreads -- set from the
        # spreads alone, never from outcomes
        seps = sorted(r["sep"] for r in rows)
        cut = (args.squeeze_cut if args.squeeze_cut is not None
               else (seps[len(seps) // 4] if seps else 0.0))
        for r in rows:
            r["squeezed"] = r["sep"] < cut
        ordered = sorted(rows, key=lambda r: r["entry_utc"])
        order = {id(r): i for i, r in enumerate(ordered)}
        half = len(rows) // 2
        trapped_share = 100 * sum(1 for r in rows if r["trapped"]) / len(rows) if rows else 0

        print(f"  squeezed = EMA13-21 gap below ${cut:.3f} "
              + ("(FIXED -- the forward test's frozen cut)" if args.squeeze_cut is not None
                 else "(this account's lowest quarter)"))
        print(f"  trapped  = {trapped_share:.0f}% of all entries happened inside the previous H1 candle")
        print(f"  whole account: {mean([r['oz'] for r in rows]):+.2f} $/oz per trade\n")

        print("  each measure alone")
        report("trapped inside the H1 candle", [r for r in rows if r["trapped"]], order, half)
        report("broken out of it", [r for r in rows if not r["trapped"]], order, half)
        report("EMAs squeezed", [r for r in rows if r["squeezed"]], order, half)
        report("EMAs apart", [r for r in rows if not r["squeezed"]], order, half)

        print("\n  both together")
        report("broken out, EMAs apart", [r for r in rows if not r["trapped"] and not r["squeezed"]], order, half)
        report("broken out, EMAs squeezed", [r for r in rows if not r["trapped"] and r["squeezed"]], order, half)
        report("trapped, EMAs apart", [r for r in rows if r["trapped"] and not r["squeezed"]], order, half)
        n, f, s = report("THE TRAP: trapped AND squeezed",
                         [r for r in rows if r["trapped"] and r["squeezed"]], order, half)

        if n < MIN_GROUP:
            verdict = f"TOO FEW ({n} trades) -- no result"
        elif f < 0 and s < 0:
            verdict = "LOSES in both halves -- passes on this account"
        elif f < 0 or s < 0:
            verdict = "loses in ONE half only -- fails"
        else:
            verdict = "still PROFITABLE in both halves -- fails; keep taking these trades"
        verdicts[account] = verdict
        print(f"\n  VERDICT for {account}: {verdict}")

    print(f"\n{'=' * 108}")
    print("THE RULE, set before the result: the trap group must LOSE money in both halves on")
    print("BOTH demo2_m3 AND live2_m3. Anything else means those trades keep being taken.")
    for account, verdict in verdicts.items():
        print(f"  {account:<10} {verdict}")
    passed = verdicts and all(v.startswith("LOSES") for v in verdicts.values()) and len(verdicts) >= 2
    print(f"\n  OVERALL: {'PASSES -- worth shadow-logging on live2' if passed else 'DOES NOT PASS'}")
    print("\nCaveat: the squeeze half was noticed on this same data this morning, so this data")
    print("cannot confirm it by itself. The H1 trap is the genuinely new question.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
