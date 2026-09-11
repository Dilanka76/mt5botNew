"""Every trade on an account, split into what the BOTS did and what was
done by hand.

User, 2026-09-11. Every other report here filters by magic number, so it
shows only one bot's trades and is blind by construction to anything
else. That is how a position at 4421.93 could not be explained on
2026-09-10: it was opened by a bot nobody was looking at, and no
magic-filtered report would ever have shown it.

This filters by nothing. It reads every deal on the account and groups by
magic:

    910003 910005 920003 920005   the four live legs
    910001 920001                 the RETIRED M1 legs — if these appear
                                  after 2026-09-10 07:17, something
                                  restarted them despite the kill switch
    0                             opened by hand
    anything else                 another platform on the same login

Accounts that share an MT5 login are queried once, not once each, since
demo1_m3 and demo1_m5 return identical deal histories.

    python scripts/who_traded.py --days 2
    python scripts/who_traded.py --days 7 --accounts demo1_m3,demo2_m3

Read-only.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")

# Magic -> what it is. Anything not listed is reported as unknown rather
# than guessed at.
KNOWN = {
    910003: "demo1_m3 (live leg)",
    910005: "demo1_m5 (live leg)",
    920003: "demo2_m3 (live leg)",
    920005: "demo2_m5 (live leg)",
    910001: "demo1_m1  *** RETIRED 2026-09-10 ***",
    920001: "demo2_m1  *** RETIRED 2026-09-10 ***",
    950001: "live2_m1  *** RETIRED 2026-09-10 ***",
    950003: "live2_m3",
    0: "OPENED BY HAND (no bot)",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m3,demo2_m3",
                   help="one per MT5 login is enough; siblings share a history")
    p.add_argument("--days", type=int, default=2)
    p.add_argument("--detail", action="store_true", help="list every trade, not just totals")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    seen_logins: set[int] = set()

    print("=" * 90)
    print(f"WHO TRADED — every deal, last {args.days} days, filtered by nothing")
    print("=" * 90)

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        if not (PROJECT_ROOT / f".env.{account}").exists():
            print(f"\n{account}: no credentials, skipped.")
            continue
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            login = connector.account_info().login
            if login in seen_logins:
                print(f"\n{account}: same login {login} as an account already shown — skipped.")
                continue
            seen_logins.add(login)
            offset = mt5_utc_offset(connector, config.symbol)
            deals = mt5.history_deals_get(since + offset, now + offset) or []

            by_position: dict[int, list] = defaultdict(list)
            for d in deals:
                if d.symbol == config.symbol:
                    by_position[d.position_id].append(d)

            groups: dict[int, list] = defaultdict(list)
            for pid, ds in by_position.items():
                ds.sort(key=lambda d: d.time)
                entry = next((d for d in ds if d.entry == 0), None)
                if entry is None:
                    continue
                profit = sum(d.profit + d.commission + d.swap for d in ds)
                closed = any(d.entry == 1 for d in ds)
                groups[entry.magic].append({
                    "pid": pid, "time": datetime.fromtimestamp(entry.time, tz=timezone.utc) - offset,
                    "dir": "BUY" if entry.type == 0 else "SELL",
                    "lots": entry.volume, "price": entry.price,
                    "profit": profit, "closed": closed,
                })

            print(f"\n{'=' * 90}")
            print(f"{account}  (MT5 login {login}, balance {connector.account_info().balance:.2f})")
            print("=" * 90)
            if not groups:
                print("  No deals at all in this window.")
                continue

            for magic in sorted(groups, key=lambda m: (m not in KNOWN, m)):
                rows = sorted(groups[magic], key=lambda r: r["time"])
                total = sum(r["profit"] for r in rows)
                still_open = sum(1 for r in rows if not r["closed"])
                label = KNOWN.get(magic, f"UNKNOWN magic {magic} — not this project")
                print(f"\n  magic {magic:<8} {label}")
                print(f"    {len(rows)} trade(s), net ${total:+.2f}"
                      + (f", {still_open} still open" if still_open else ""))
                if args.detail or magic not in KNOWN or magic in (0, 910001, 920001, 950001):
                    for r in rows:
                        print(f"      {r['time'].astimezone(COLOMBO):%d %b %H:%M:%S}  "
                              f"{r['dir']:<4} {r['lots']:>5} lots at {r['price']:>9.2f}  "
                              f"${r['profit']:>+8.2f}"
                              + ("" if r["closed"] else "   STILL OPEN"))

            bot_pl = sum(sum(r["profit"] for r in rows)
                         for m, rows in groups.items() if m != 0)
            hand_pl = sum(r["profit"] for r in groups.get(0, []))
            bot_n = sum(len(rows) for m, rows in groups.items() if m != 0)
            hand_n = len(groups.get(0, []))
            print(f"\n  ---- {account} summary ----")
            print(f"    bots       : {bot_n:>3} trade(s)   ${bot_pl:>+9.2f}")
            print(f"    by hand    : {hand_n:>3} trade(s)   ${hand_pl:>+9.2f}")
            print(f"    everything : {bot_n + hand_n:>3} trade(s)   ${bot_pl + hand_pl:>+9.2f}")
            if hand_n:
                print(f"    NOTE manual trades are closed on sight by every bot")
                print(f"         (reject_manual_trades), usually within a second or two.")
            retired = [m for m in groups if m in (910001, 920001, 950001)]
            if retired:
                print(f"    *** RETIRED legs traded in this window: "
                      f"{', '.join(str(m) for m in retired)}")
                print(f"        Check the timestamps above against the retirement at")
                print(f"        2026-09-10 07:17 — anything after it means a kill switch failed.")
        finally:
            connector.disconnect()


if __name__ == "__main__":
    main()
