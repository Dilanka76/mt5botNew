"""Does the clock tell us where the losses are -- the Asian box, and the sessions?

From an outside gold playbook the user brought, 2026-09-23: "Gold routinely
compresses into a tight sideways box during the Asian hours. Mark the
highest high and lowest low of the Asian session -- that forms a
guaranteed sideways range. When London or New York opens, the price is
swept right outside it to grab stops."

Worth one test because it is the one idea in that list with INDEPENDENT
support from this project's own accounts: the 2026-09-07 session study
found the Asian window the WORST of the day on 3 of 4 accounts, and it
never got a walk-forward check ([[project_market_session_analysis]]).

It is also a different shape of range from the one demo2 is skipping now.
Ours needs two touches of a level; this one is anchored to the clock.

WHAT IS MEASURED, all from the entry's own time and price:

  session       ASIAN 00:00-07:00 UTC, LONDON 07:00-12:00,
                OVERLAP 12:00-16:00, NEW YORK 16:00-21:00, LATE otherwise
  in the box    for entries after 07:00 UTC: the entry price sits between
                that day's Asian high and Asian low, the "box" the
                playbook says gets swept
  where in it   how far up that box the entry sat, in quarters

Then the two rules it implies are priced: skip Asian-session entries, and
skip entries inside the Asian box.

THE BAR, set before the first run: a rule is worth building only if it
GAINS money in BOTH halves, on BOTH M3 accounts AND both M5 accounts.
Skipped trades score zero; everything else stays as it really happened,
and a skipped entry would have left the bot flat, so later reversal
re-entries would differ -- that alternative history is not simulated.

    python scripts/session_test.py --since "2026-08-25 00:00:00"
    python scripts/session_test.py --offset-hours 3      (weekends)

Read-only.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0
ASIAN_START, ASIAN_END = time(0, 0), time(7, 0)      # UTC
SESSIONS = [("ASIAN", 0, 7), ("LONDON", 7, 12), ("OVERLAP", 12, 16), ("NEW YORK", 16, 21)]


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


def session_of(when: datetime) -> str:
    h = when.hour
    for name, start, end in SESSIONS:
        if start <= h < end:
            return name
    return "LATE"


def show(label: str, rows: list, order: dict, half: int) -> None:
    if not rows:
        print(f"    {label:<24} none")
        return
    w = sum(1 for r in rows if r["oz"] > 0)
    f = mean([r["oz"] for r in rows if order[id(r)] < half])
    s = mean([r["oz"] for r in rows if order[id(r)] >= half])
    flag = "   <- loses in both halves" if len(rows) >= 20 and f < 0 and s < 0 else ""
    print(f"    {label:<24} {len(rows):>4} trades {100 * w / len(rows):>4.0f}% won "
          f"{mean([r['oz'] for r in rows]):>+6.2f} $/oz   halves {f:>+6.2f} / {s:>+6.2f}   "
          f"{money(sum(r['profit'] for r in rows)):>10}{flag}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("THE CLOCK -- sessions, and the Asian box the playbook says gets swept")
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
            m15 = get_ohlc_range(connector, config.symbol, "M15",
                                 since - timedelta(days=2), now, offset)
        finally:
            connector.disconnect()

        # that day's Asian high/low, from candles CLOSED inside the window
        asian: dict = {}
        for ts, row in m15.iterrows():
            if ASIAN_START <= ts.time() < ASIAN_END:
                hi, lo = asian.get(ts.date(), (float("-inf"), float("inf")))
                asian[ts.date()] = (max(hi, float(row["high"])), min(lo, float(row["low"])))

        rows = []
        for t in raw:
            entry = t["entry_time"].astimezone(timezone.utc)
            if entry < since:
                continue
            price = float(t["entry_price"])
            box = asian.get(entry.date())
            after_asia = entry.time() >= ASIAN_END
            inside = where = None
            if box and after_asia and box[0] > box[1]:
                inside = box[1] <= price <= box[0]
                where = (price - box[1]) / (box[0] - box[1])
            rows.append({"entry": entry, "profit": float(t["profit"]),
                         "oz": float(t["profit"]) / (float(t["volume"]) * OZ_PER_LOT),
                         "session": session_of(entry), "inside": inside, "where": where})

        if not rows:
            print(f"\n{account}: no trades")
            continue
        order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry"]))}
        half = len(rows) // 2
        real = sum(r["profit"] for r in rows)

        print(f"\n{'=' * 100}\n{account}   {config.timeframe}   {len(rows)} trades   "
              f"really {money(real)}\n{'=' * 100}")

        print("  by session (UTC)")
        for name, start, end in SESSIONS:
            show(f"{name} {start:02d}-{end:02d}", [r for r in rows if r["session"] == name],
                 order, half)
        show("LATE 21-24", [r for r in rows if r["session"] == "LATE"], order, half)

        print("\n  the Asian box, for entries after 07:00 UTC")
        show("inside the box", [r for r in rows if r["inside"] is True], order, half)
        show("outside it", [r for r in rows if r["inside"] is False], order, half)
        quarters = [r for r in rows if r["where"] is not None]
        if len(quarters) >= 40:
            for i, name in enumerate(("bottom quarter", "lower middle",
                                      "upper middle", "top quarter")):
                lo, hi = i / 4, (i + 1) / 4
                sel = [r for r in quarters if lo <= r["where"] < hi] if i < 3 else \
                      [r for r in quarters if r["where"] >= lo]
                show(f"  {name}", sel, order, half)

        print("\n  the rules that implies")
        for label, skipped in (("skip ASIAN entries", [r for r in rows if r["session"] == "ASIAN"]),
                               ("skip inside-the-box", [r for r in rows if r["inside"] is True])):
            if not skipped:
                continue
            d1 = -sum(r["profit"] for r in skipped if order[id(r)] < half)
            d2 = -sum(r["profit"] for r in skipped if order[id(r)] >= half)
            gone = sum(r["profit"] for r in skipped)
            print(f"    {label:<22} skips {len(skipped):>3} trades -> {money(-gone):>10} "
                  f"   halves {money(d1)} / {money(d2)}")

    print(f"\n{'=' * 100}")
    print("THE BAR (set before this run): a rule is worth building only if it GAINS in BOTH")
    print("halves, on BOTH M3 accounts AND both M5 accounts. Sessions are UTC; the broker's")
    print("clock is UTC+3 and Colombo is UTC+5:30, so 'ASIAN 00-07' is 05:30-12:30 Colombo.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
