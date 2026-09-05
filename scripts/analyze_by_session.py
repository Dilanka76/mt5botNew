"""How does the bot perform in each MARKET SESSION -- Asian, London, New
York, and the London/NY overlap?

User's question (2026-09-04): which times are actually good to trade and
which are not, reported in Sri Lanka time.

Why sessions rather than another hand-picked window: the existing
time-of-day finding (08:00-12:00 broker time is weak) was chosen because
it looked worst in our own data -- a classic way to fit noise. Market
sessions are defined by how the market actually works, independent of
our results, so a finding that lines up with them is far harder to
dismiss as coincidence. And the weak window turns out to BE the late
Asian session, which is the first hint that these separate results
(quiet volume, weak window, choppy regime) are one phenomenon.

Times: the bot's candle timestamps are raw BROKER time (UTC+3, see
bot/data/market_data.get_ohlc, no offset correction). Sri Lanka is
UTC+5:30, so Colombo = broker + 2:30. Both are printed.

Sessions overlap by nature (London and New York genuinely run at the
same time for part of the day), so a trade can be counted in more than
one. The overlap is reported separately as the period both are open.

    python scripts/analyze_by_session.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read real closed-trade history.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

# (name, start_hour, end_hour) in BROKER time (UTC+3). End is exclusive.
SESSIONS = [
    ("Asian",             3, 12),
    ("London",           10, 19),
    ("New York",         15, 24),
    ("London/NY overlap", 15, 19),
]
BROKER_TO_COLOMBO = timedelta(hours=2, minutes=30)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def colombo_label(start_h: int, end_h: int) -> str:
    def conv(h: int) -> str:
        total = h * 60 + 150          # +2:30
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"
    return f"{conv(start_h)}-{conv(end_h)}"


def summarize(label: str, profits: list[float], extra: str = "") -> None:
    if not profits:
        print(f"    {label:<34} no trades")
        return
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    total = sum(profits)
    print(f"    {label:<34} n={n:<4} {100*wins/n:5.1f}% win   "
          f"total ${total:+9.2f}   avg ${total/n:+7.2f}/trade{extra}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    print("Session times -- broker (UTC+3) and Sri Lanka (UTC+5:30):")
    for name, s, e in SESSIONS:
        print(f"  {name:<20} broker {s:02d}:00-{e:02d}:00    Sri Lanka {colombo_label(s, e)}")
    print()

    grand: dict[str, list[float]] = {name: [] for name, _, _ in SESSIONS}
    grand_hour: dict[int, list[float]] = {}

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
        finally:
            connector.disconnect()

        trades = [t for t in raw if t["entry_time"].astimezone(timezone.utc) >= since]
        if not trades:
            print(f"{account}: no trades.\n")
            continue

        # entry_time comes back in Colombo; broker hour = Colombo - 2:30
        by_session: dict[str, list[float]] = {name: [] for name, _, _ in SESSIONS}
        by_hour: dict[int, list[float]] = {}
        for t in trades:
            broker_dt = t["entry_time"] - BROKER_TO_COLOMBO
            h = broker_dt.hour
            by_hour.setdefault(h, []).append(t["profit"])
            grand_hour.setdefault(h, []).append(t["profit"])
            for name, s, e in SESSIONS:
                if s <= h < e:
                    by_session[name].append(t["profit"])
                    grand[name].append(t["profit"])

        total = sum(t["profit"] for t in trades)
        wins = sum(1 for t in trades if t["profit"] > 0)
        print(f"{'=' * 82}\n{account}: {len(trades)} trades, {100*wins/len(trades):.1f}% win, "
              f"total ${total:+.2f}\n{'=' * 82}")
        for name, s, e in SESSIONS:
            summarize(f"{name} ({colombo_label(s, e)} SL)", by_session[name])
        print()

    print(f"{'=' * 82}\nALL ACCOUNTS COMBINED\n{'=' * 82}")
    for name, s, e in SESSIONS:
        summarize(f"{name} ({colombo_label(s, e)} SL)", grand[name])

    print(f"\n  Hour by hour (Sri Lanka time), all accounts:")
    for h in sorted(grand_hour):
        profits = grand_hour[h]
        if len(profits) < 3:      # too few to mean anything
            continue
        total = sum(profits)
        wins = sum(1 for p in profits if p > 0)
        colombo_h = (h * 60 + 150) // 60 % 24
        bar = "+" * min(int(abs(total) / 40), 20)
        sign = "" if total >= 0 else "-"
        print(f"    {colombo_h:02d}:00 SL  n={len(profits):<4} {100*wins/len(profits):5.1f}% win  "
              f"${total:+9.2f}  {sign}{bar}")


if __name__ == "__main__":
    main()
