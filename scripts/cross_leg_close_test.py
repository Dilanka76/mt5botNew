"""When one leg opens OPPOSITE to the other leg's open trade, close that trade.

User's idea, 2026-09-22: "first opens an M3 buy, then the M5 opens -- if the
second trade goes opposite to the first, close the first one, profit or
loss. Only for BUY-then-SELL or SELL-then-BUY."

Today M3 and M5 trade the same account as if blind to each other. When
they disagree, the account holds a BUY and a SELL on the same gold: zero
net exposure, two spreads paid. The rule closes the older position the
moment the newer leg goes the other way.

WHY THIS TEST IS UNUSUALLY EXACT: the price the older trade would have
closed at is the newer trade's own entry price -- same symbol, same
second, and the right side of the book (a BUY closes on the bid, which is
exactly where the opposite SELL opened; a SELL closes on the ask, where
the opposite BUY opened). No candles, no guessing about the order of
moves inside one.

And the rest of the history does not shift: the leg whose trade was
closed early would have been flat at the moment its own cross came, so it
enters on that cross exactly as it really did -- entries are unchanged.

Three versions are scored:
  BOTH       the user's rule -- whichever leg is newer closes the older
  M5 -> M3   only a new M5 trade closes an opposite M3 trade
  M3 -> M5   only a new M3 trade closes an opposite M5 trade

Each is shown in the first and second half of the trades, per account
pair. A version is worth building only if it helps in BOTH halves, on
demo2 AND on live2 -- the same bar every rule here has had to clear.

    python scripts/cross_leg_close_test.py
    python scripts/cross_leg_close_test.py --since "2026-09-09 00:00:00"

Read-only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

OZ_PER_LOT = 100.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pairs", default="demo2_m3:demo2_m5,live2_m3:live2_m5",
                   help="M3 leg : M5 leg, comma separated")
    p.add_argument("--since", default="2026-09-09 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def trades_for(account: str, since: datetime, offset_hours) -> list[dict]:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=offset_hours) if offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                      since, datetime.now(timezone.utc), offset)
    finally:
        connector.disconnect()
    out = []
    for t in raw:
        entry_utc = t["entry_time"].astimezone(timezone.utc)
        if entry_utc < since:
            continue
        sign = 1.0 if t["direction"] == "BUY" else -1.0
        vol = float(t["volume"])
        entry, exit_px, profit = float(t["entry_price"]), float(t["exit_price"]), float(t["profit"])
        out.append({"entry": entry_utc, "exit": t["exit_time"].astimezone(timezone.utc),
                    "dir": t["direction"], "sign": sign, "vol": vol,
                    "entry_px": entry, "profit": profit,
                    "cost": profit - sign * (exit_px - entry) * vol * OZ_PER_LOT})
    return sorted(out, key=lambda r: r["entry"])


def early_close(trade: dict, other_leg: list[dict]):
    """The first trade of the other leg opened OPPOSITE while `trade` was
    open, or None."""
    for o in other_leg:
        if o["entry"] <= trade["entry"]:
            continue
        if o["entry"] >= trade["exit"]:
            break
        if o["dir"] != trade["dir"]:
            return o
    return None


def score(rows: list[dict], other: list[dict], applies: bool) -> list[tuple]:
    """(trade, new_profit, closing_trade) for every trade of this leg."""
    out = []
    for t in rows:
        hit = early_close(t, other) if applies else None
        if hit is None:
            out.append((t, t["profit"], None))
        else:
            alt = t["sign"] * (hit["entry_px"] - t["entry_px"]) * t["vol"] * OZ_PER_LOT + t["cost"]
            out.append((t, alt, hit))
    return out


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    print("=" * 100)
    print("CLOSE THE OLDER LEG WHEN THE OTHER LEG OPENS OPPOSITE -- replayed on real trades")
    print(f"since {since:%Y-%m-%d %H:%M} UTC.  Close price = the opposite trade's own entry price.")
    print("=" * 100)

    for pair in args.pairs.split(","):
        m3_name, m5_name = [validate_account_name(x.strip()) for x in pair.split(":")]
        m3 = trades_for(m3_name, since, args.offset_hours)
        m5 = trades_for(m5_name, since, args.offset_hours)
        if not m3 or not m5:
            print(f"\n{m3_name} + {m5_name}: not enough trades")
            continue
        actual = sum(t["profit"] for t in m3 + m5)
        everything = sorted(m3 + m5, key=lambda r: r["entry"])
        cut = everything[len(everything) // 2]["entry"]

        print(f"\n{m3_name} ({len(m3)} trades) + {m5_name} ({len(m5)} trades)   "
              f"really: {money(actual)}")
        print(f"  {'version':<12} {'closed early':>13} {'net':>12} {'vs real':>11} "
              f"{'1st half':>11} {'2nd half':>11}   of the early closes")
        for label, m5_closes_m3, m3_closes_m5 in (("BOTH (yours)", True, True),
                                                  ("M5 -> M3", True, False),
                                                  ("M3 -> M5", False, True)):
            scored = score(m3, m5, m5_closes_m3) + score(m5, m3, m3_closes_m5)
            net = sum(p for _, p, _ in scored)
            changed = [(t, p) for t, p, hit in scored if hit is not None]
            d1 = sum(p - t["profit"] for t, p in changed if t["entry"] < cut)
            d2 = sum(p - t["profit"] for t, p in changed if t["entry"] >= cut)
            saved = sum(1 for t, p in changed if p > t["profit"])
            print(f"  {label:<12} {len(changed):>13} {money(net):>12} {money(net - actual):>11} "
                  f"{money(d1):>11} {money(d2):>11}   {saved} better, {len(changed) - saved} worse")

        print("  A version is worth building only if 'vs real' is positive in BOTH halves,")
        print("  on demo2 AND on live2.")

    print(f"\n{'=' * 100}")
    print("Entries are unchanged by construction: a leg closed early is flat when its own cross")
    print("comes, and enters on it exactly as it really did.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
