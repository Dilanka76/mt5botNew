"""Every trade ever placed on a live account, reviewed from the broker's
own records.

User, 2026-09-19: "check the overall trades which are placed in the live
account".

Reads the account's FULL deal history, filtered by nothing, so it sees
bot trades, trades opened by hand, deposits and withdrawals alike. Exit
reasons come from the broker's own deal record, not from our logs:

    TP            the broker filled the take-profit
    BACKSTOP      the broker filled the $30/$35 backstop stop
    bot close     our bot closed it (the opposite-cross swap)
    by hand       closed from the desktop terminal, the phone, or the web
    STOP-OUT      the broker closed it for lack of margin

Checks the books balance: deposits + every trade's P/L must equal the
balance MT5 reports. If they do not, something is missing from this view
and the report says so instead of pretending.

WEEKENDS. The broker's clock offset from UTC is measured from live ticks,
which do not flow while the market is closed. On a weekend pass
--offset-hours 3 (BlackBull's server clock is UTC+3, measured many times
on weekdays in this project).

    python scripts/live_review.py
    python scripts/live_review.py --offset-hours 3 --detail

Read-only.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import StaleTickError, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")

LEGS = {950003: "live2_m3", 950005: "live2_m5", 0: "OPENED BY HAND"}

# MT5 deal.reason values (ENUM_DEAL_REASON).
EXIT_REASON = {0: "by hand (desktop)", 1: "by hand (phone)", 2: "by hand (web)",
               3: "bot close", 4: "BACKSTOP", 5: "TP", 6: "STOP-OUT"}
DEAL_TYPE_BALANCE = 2
ENTRY_IN, ENTRY_OUT, ENTRY_OUT_BY = 0, 1, 3


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="live2_m3", type=validate_account_name,
                   help="any one leg of the login; legs share one history")
    p.add_argument("--since", default="2026-09-01", help="YYYY-MM-DD, true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset from UTC; needed when the market is closed (3)")
    p.add_argument("--detail", action="store_true", help="list every trade")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def summary(label: str, rows: list) -> None:
    if not rows:
        print(f"\n  {label}: no closed trades")
        return
    wins = [r for r in rows if r["net"] > 0]
    losses = [r for r in rows if r["net"] <= 0]
    gw = sum(r["net"] for r in wins)
    gl = sum(r["net"] for r in losses)
    streak = best = 0
    for r in sorted(rows, key=lambda r: r["closed"]):
        streak = streak + 1 if r["net"] <= 0 else 0
        best = max(best, streak)
    hold = sorted((r["closed"] - r["opened"]).total_seconds() / 60 for r in rows)
    print(f"\n  {label}")
    print(f"    trades        {len(rows)}   ({len(wins)} won, {len(losses)} lost, "
          f"win rate {100 * len(wins) / len(rows):.0f}%)")
    print(f"    net           {money(gw + gl)}   (gross won {money(gw)}, gross lost {money(gl)})")
    if wins and losses:
        print(f"    profit factor {gw / abs(gl):.2f}   (above 1.00 = making money)")
    if wins:
        print(f"    average win   {money(gw / len(wins))}    biggest win  "
              f"{money(max(r['net'] for r in wins))}")
    if losses:
        print(f"    average loss  {money(gl / len(losses))}    biggest loss "
              f"{money(min(r['net'] for r in losses))}")
    print(f"    costs         {money(sum(r['costs'] for r in rows))}   (commission + swap + fees, "
          f"already inside net)")
    print(f"    longest losing streak {best}    median time in trade {hold[len(hold) // 2]:.0f} min")
    reasons = Counter(r["reason"] for r in rows)
    print("    how trades closed:")
    for reason, n in reasons.most_common():
        rs = [r for r in rows if r["reason"] == reason]
        w = sum(1 for r in rs if r["net"] > 0)
        print(f"      {reason:<20} {n:>4}   {w:>3} won   {money(sum(r['net'] for r in rs)):>11}")
    for d in ("BUY", "SELL"):
        ds = [r for r in rows if r["dir"] == d]
        if ds:
            w = [r for r in ds if r["net"] > 0]
            print(f"    {d:<5} {len(ds):>4} trades   {len(w):>3} won "
                  f"({100 * len(w) / len(ds):>3.0f}%)   {money(sum(r['net'] for r in ds)):>11}"
                  f"   {money(sum(r['net'] for r in ds) / len(ds))}/trade")
    lots = Counter(r["lots"] for r in rows)
    print("    lot sizes used: " + ", ".join(f"{v:g} x{n}" for v, n in sorted(lots.items())))


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        info = connector.account_info()
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else mt5_utc_offset(connector, config.symbol))
        deals = list(mt5.history_deals_get(since + offset, now + offset + timedelta(days=1)) or [])
        open_positions = list(mt5.positions_get() or [])
    finally:
        connector.disconnect()

    def utc(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc) - offset

    deals.sort(key=lambda d: d.time)
    cash = [d for d in deals if d.type == DEAL_TYPE_BALANCE]
    by_pos: dict[int, list] = defaultdict(list)
    for d in deals:
        if d.type != DEAL_TYPE_BALANCE and d.position_id:
            by_pos[d.position_id].append(d)

    trades, still_open = [], []
    # Costs already charged on trades still open. This broker books the
    # whole round-trip commission on the ENTRY deal, so an open trade has
    # already moved the balance -- leaving it out made the books check
    # report "-$0.48 missing" with two positions open (2026-09-21), which
    # was simply their entry commission.
    open_costs = 0.0
    for pid, ds in by_pos.items():
        entry = next((d for d in ds if d.entry == ENTRY_IN), None)
        exits = [d for d in ds if d.entry in (ENTRY_OUT, ENTRY_OUT_BY)]
        if entry is None:
            continue            # opened before --since
        row = {"pid": pid, "magic": entry.magic, "symbol": entry.symbol,
               "dir": "BUY" if entry.type == 0 else "SELL", "lots": entry.volume,
               "open_px": entry.price, "opened": utc(entry.time)}
        if not exits:
            open_costs += sum(d.commission + d.swap + getattr(d, "fee", 0.0) for d in ds)
            still_open.append(row)
            continue
        costs = sum(d.commission + d.swap + getattr(d, "fee", 0.0) for d in ds)
        row.update(close_px=exits[-1].price, closed=utc(exits[-1].time),
                   reason=EXIT_REASON.get(exits[-1].reason, f"reason {exits[-1].reason}"),
                   net=sum(d.profit for d in ds) + costs, costs=costs)
        trades.append(row)
    trades.sort(key=lambda r: r["closed"])

    print("=" * 92)
    print(f"LIVE ACCOUNT REVIEW   login {info.login}   {info.server}   since {since:%Y-%m-%d}")
    print(f"offset {'measured' if args.offset_hours is None else 'given'}: "
          f"broker clock = UTC{offset.total_seconds() / 3600:+g}h")
    print("=" * 92)

    # ---- the books ----------------------------------------------------
    deposited = sum(d.profit for d in cash)
    traded = sum(r["net"] for r in trades)
    print("\n  MONEY IN / OUT")
    for d in cash:
        t = utc(d.time)
        print(f"    {t:%Y-%m-%d %H:%M} UTC ({t.astimezone(COLOMBO):%d %b %H:%M} SL)  "
              f"{'deposit' if d.profit > 0 else 'withdrawal':<10} {money(d.profit):>11}  {d.comment}")
    print(f"    total deposited (net) {money(deposited)}")
    print(f"    closed-trade P/L      {money(traded)}")
    print(f"    balance now           ${info.balance:,.2f}    equity ${info.equity:,.2f}")
    if deposited:
        print(f"    return on deposits    {100 * traded / deposited:+.1f}%")
    if abs(open_costs) >= 0.005:
        print(f"    costs on open trades  {money(open_costs)}   (commission charged at entry)")
    gap = info.balance - (deposited + traded + open_costs)
    if abs(gap) > 0.05:
        print(f"    *** BOOKS DO NOT BALANCE by {money(gap)}: something before {since:%Y-%m-%d} or")
        print("        outside this view moved money. Re-run with an earlier --since.")
    else:
        print("    books balance: deposits + trades = balance, nothing missing")

    # ---- per leg ------------------------------------------------------
    summary("ALL TRADES", trades)
    for magic in sorted({r["magic"] for r in trades}, key=lambda m: (m not in LEGS, m)):
        label = LEGS.get(magic, f"UNKNOWN magic {magic} -- not a live2 leg")
        summary(f"{label}  (magic {magic})", [r for r in trades if r["magic"] == magic])

    # ---- balance path and drawdown ------------------------------------
    events = [(utc(d.time), d.profit, True) for d in cash] + \
             [(r["closed"], r["net"], False) for r in trades]
    events.sort(key=lambda e: e[0])
    bal = peak = 0.0
    worst_dd = worst_pct = 0.0
    worst_at = None
    for t, amount, is_cash in events:
        bal += amount
        if is_cash:
            peak += amount          # money moved in/out is not a gain or a drawdown
        peak = max(peak, bal)
        dd = peak - bal
        if dd > worst_dd:
            worst_dd, worst_pct, worst_at = dd, 100 * dd / peak if peak else 0.0, t
    print("\n  DRAWDOWN (closed trades only)")
    if worst_at:
        print(f"    deepest fall from a high: {money(-worst_dd)} ({worst_pct:.1f}% of that high), "
              f"bottom at {worst_at.astimezone(COLOMBO):%d %b %H:%M} SL")
    else:
        print("    none -- the balance never fell below a previous high")

    # ---- day by day ---------------------------------------------------
    print("\n  DAY BY DAY (Colombo date the trade closed)")
    print(f"    {'day':<11} {'M3':>18} {'M5':>18} {'total':>12}  balance after")
    run = 0.0
    cash_by_day = defaultdict(float)
    for d in cash:
        cash_by_day[utc(d.time).astimezone(COLOMBO).date()] += d.profit
    days = sorted({r["closed"].astimezone(COLOMBO).date() for r in trades} | set(cash_by_day))
    for day in days:
        rs = [r for r in trades if r["closed"].astimezone(COLOMBO).date() == day]
        cells = []
        for magic in (950003, 950005):
            leg = [r for r in rs if r["magic"] == magic]
            cells.append(f"{len(leg):>2}t {sum(1 for r in leg if r['net'] > 0):>2}W "
                         f"{money(sum(r['net'] for r in leg)):>9}" if leg else f"{'-':>18}")
        total = sum(r["net"] for r in rs)
        run += cash_by_day[day] + total
        extra = f"   (+ deposit {money(cash_by_day[day])})" if cash_by_day[day] else ""
        print(f"    {day!s:<11} {cells[0]:>18} {cells[1]:>18} {money(total):>12}  "
              f"${run:,.2f}{extra}")

    # ---- anything that should not be there ----------------------------
    odd = [r for r in trades if r["magic"] not in (950003, 950005)]
    wrong_sym = [r for r in trades if r["symbol"] != config.symbol]
    print("\n  CHECKS")
    print(f"    trades not from a live2 bot: {len(odd)}"
          + ("   <- see list below" if odd else ""))
    print(f"    trades on a symbol other than {config.symbol}: {len(wrong_sym)}")
    backstops = [r for r in trades if r["reason"] == "BACKSTOP"]
    print(f"    backstop fills: {len(backstops)}")
    print(f"    open right now: {len(open_positions)}")
    for p in open_positions:
        print(f"      {'BUY' if p.type == 0 else 'SELL'} {p.volume:g} lots at {p.price_open:.2f}  "
              f"magic {p.magic}  floating {money(p.profit)}")

    if args.detail or odd:
        print("\n  TRADES" + ("" if args.detail else " NOT FROM A LIVE2 BOT"))
        for r in (trades if args.detail else odd):
            print(f"    {r['opened'].astimezone(COLOMBO):%d %b %H:%M}-"
                  f"{r['closed'].astimezone(COLOMBO):%H:%M} SL  "
                  f"{LEGS.get(r['magic'], r['magic'])!s:<9} {r['dir']:<4} {r['lots']:>5g} "
                  f"{r['open_px']:>9.2f} -> {r['close_px']:>9.2f}  {money(r['net']):>9}  {r['reason']}")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- the broker clock offset cannot be measured from ticks.")
        print("Re-run with:  --offset-hours 3")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
