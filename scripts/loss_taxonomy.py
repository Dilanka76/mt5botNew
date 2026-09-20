"""WHY did each losing trade lose? One bucket per loser, from its real path.

Mission set 2026-09-20: grow the live account, and to do that cut the
dollars lost and the number of losers. Before any filter is designed,
the losses have to be NAMED. About a dozen entry filters have already
been tested and killed here (ADX, colour+volume, efficiency ratio,
session, volatility, EMA50/100). Guessing at another one is not a plan.

So every loser is replayed against its real candles and put in exactly
one bucket, by precedence:

  NEARLY WON    got >=70% of the way to its target, then reversed.
                An EXIT/management problem. The entry was right.
  DEAD ON ARRIVAL   never showed even $1 of profit. The cross was false
                from its first candle. This is the entry-quality bucket.
  SHOCK         its entry candle was >=3x the normal candle range for
                that day -- a spike, usually news. Not a normal entry.
  CHOP CHURN    entered within 2 candles of the previous trade's exit:
                the market is ranging and the cross keeps flipping.
  SLOW BLEED    everything else: drifted against us until the cross.

THE RULE THIS SCRIPT ENFORCES, learned the expensive way (the colour
filter cost $192 live): a bucket is only worth attacking if avoiding it
GAINS money. So every bucket reports its winners too, and its NET. A
bucket holding 20 losers and 25 winners is not a problem to be filtered
-- it is just where this strategy lives. Ranking by "total lost" alone
is the arithmetic artifact that has burned this project before.

    python scripts/loss_taxonomy.py --since "2026-09-09 00:00:00"
    python scripts/loss_taxonomy.py --since "2026-09-09 00:00:00" --offset-hours 3

Read-only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

COLOMBO = ZoneInfo("Asia/Colombo")
OZ_PER_LOT = 100.0

NEARLY_WON_SHARE = 0.70     # of the trade's own target
DEAD_ON_ARRIVAL_USD = 1.00  # never showed this much profit
SHOCK_RANGE_MULT = 3.0      # entry candle vs the day's median candle range
CHOP_GAP_CANDLES = 2        # entered within this many candles of the last exit

BUCKETS = ["NEARLY WON", "DEAD ON ARRIVAL", "SHOCK", "CHOP CHURN", "SLOW BLEED"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="live2_m3,live2_m5,demo2_m3,demo2_m5")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    p.add_argument("--detail", action="store_true", help="list every losing trade")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def read_entries(account: str) -> list:
    """Logged entries: the target this trade was given, and its gap."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "trade_entered":
            continue
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        out.append({"ts": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                    "direction": e.get("direction"), "target": e.get("target_usd"),
                    "gap": e.get("gap"), "aligned": e.get("htf_aligned")})
    return out


def match_entry(entries: list, direction: str, when: datetime) -> dict | None:
    best, best_d = None, 90.0
    for e in entries:
        if e["direction"] != direction:
            continue
        d = abs((e["ts"] - when).total_seconds())
        if d <= best_d:
            best, best_d = e, d
    return best


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 94)
    print("WHY THE LOSERS LOST -- every losing trade in exactly one bucket")
    print(f"since {since:%Y-%m-%d %H:%M} UTC")
    print("=" * 94)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            # hand it the offset too: get_ohlc_range measures its own
            # otherwise, which throws on a weekend even when one was given.
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(days=1), now, offset)
        finally:
            connector.disconnect()

        entries = read_entries(account)
        bar = timedelta(minutes=minutes_for(config.timeframe))
        ranges = (df["high"] - df["low"]).tolist()
        normal_range = statistics.median(ranges) if ranges else 0.0

        rows = []
        prev_exit: datetime | None = None
        for t in sorted(raw, key=lambda x: x["entry_time"]):
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            firm = df[(df.index > entry_utc - bar) & (df.index + bar <= exit_utc)]
            entry_candle = df[(df.index > entry_utc - bar) & (df.index <= entry_utc)]
            if entry_candle.empty:
                continue
            entry = float(t["entry_price"])
            exit_px = float(t["exit_price"])
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            at_exit = sign * (exit_px - entry)
            if firm.empty:
                mfe = mae = 0.0
            elif sign > 0:
                mfe = float(firm["high"].max()) - entry
                mae = entry - float(firm["low"].min())
            else:
                mfe = entry - float(firm["low"].min())
                mae = float(firm["high"].max()) - entry
            mfe = max(mfe, at_exit, 0.0)
            mae = max(mae, -at_exit, 0.0)

            logged = match_entry(entries, t["direction"], entry_utc)
            target = (logged or {}).get("target") or config.take_profit_usd
            gap_candles = ((entry_utc - prev_exit) / bar) if prev_exit else None
            e_range = float(entry_candle["high"].iloc[-1] - entry_candle["low"].iloc[-1])

            # A REVERSAL RE-ENTRY: opened within a minute of the previous
            # trade's close, i.e. the swap flipped straight into the other
            # direction. 92% of live2's losses (2026-09-16..19) came in runs
            # of 3+ losses, and those runs are made of these. Spacing and
            # cooldown rules are already ruled out (2026-09-03) -- this asks
            # a different question: are the reversals themselves profitable?
            reentry = gap_candles is not None and gap_candles * bar.total_seconds() <= 60
            row = {"reentry": reentry, "profit": float(t["profit"]), "volume": float(t["volume"]),
                   "dir": t["direction"], "mfe": mfe, "mae": mae, "target": float(target),
                   "entry_utc": entry_utc, "exit_utc": exit_utc,
                   "entry_px": entry, "e_range": e_range,
                   "since_prev": gap_candles, "gap": (logged or {}).get("gap"),
                   "aligned": (logged or {}).get("aligned"),
                   "minutes": (exit_utc - entry_utc).total_seconds() / 60}
            # one bucket per trade, in this order
            if mfe >= NEARLY_WON_SHARE * float(target):
                row["bucket"] = "NEARLY WON"
            elif mfe < DEAD_ON_ARRIVAL_USD:
                row["bucket"] = "DEAD ON ARRIVAL"
            elif normal_range and e_range >= SHOCK_RANGE_MULT * normal_range:
                row["bucket"] = "SHOCK"
            elif gap_candles is not None and gap_candles <= CHOP_GAP_CANDLES:
                row["bucket"] = "CHOP CHURN"
            else:
                row["bucket"] = "SLOW BLEED"
            rows.append(row)
            prev_exit = exit_utc

        print(f"\n{'=' * 94}")
        print(f"{account}   {config.timeframe}   target ${config.take_profit_usd:.2f}"
              f"   normal candle range ${normal_range:.2f}")
        print("=" * 94)
        if not rows:
            print("  no trades")
            continue
        losers = [r for r in rows if r["profit"] <= 0]
        winners = [r for r in rows if r["profit"] > 0]
        print(f"  {len(rows)} trades: {len(winners)} won {money(sum(r['profit'] for r in winners))}, "
              f"{len(losers)} lost {money(sum(r['profit'] for r in losers))}   "
              f"net {money(sum(r['profit'] for r in rows))}")

        print(f"\n  {'bucket':<17} {'losers':>7} {'lost':>11} {'avg':>9} | "
              f"{'winners':>8} {'won':>11} | {'BUCKET NET':>12}")
        for b in BUCKETS:
            bl = [r for r in losers if r["bucket"] == b]
            bw = [r for r in winners if r["bucket"] == b]
            if not bl and not bw:
                continue
            lost = sum(r["profit"] for r in bl)
            won = sum(r["profit"] for r in bw)
            print(f"  {b:<17} {len(bl):>7} {money(lost):>11} "
                  f"{money(lost / len(bl)) if bl else '-':>9} | "
                  f"{len(bw):>8} {money(won):>11} | {money(lost + won):>12}")
        print("  A bucket is only worth attacking if its NET is negative -- avoiding a")
        print("  bucket means giving up its winners too.")

        print(f"\n  {'entry type':<17} {'losers':>7} {'lost':>11} {'avg':>9} | "
              f"{'winners':>8} {'won':>11} | {'NET':>12}")
        for label, want in (("REVERSAL re-entry", True), ("fresh entry", False)):
            bl = [r for r in losers if r["reentry"] is want]
            bw = [r for r in winners if r["reentry"] is want]
            if not bl and not bw:
                continue
            lost, won = sum(r["profit"] for r in bl), sum(r["profit"] for r in bw)
            n = len(bl) + len(bw)
            print(f"  {label:<17} {len(bl):>7} {money(lost):>11} "
                  f"{money(lost / len(bl)) if bl else '-':>9} | "
                  f"{len(bw):>8} {money(won):>11} | {money(lost + won):>12}"
                  f"   {money((lost + won) / n)}/trade")
        print("  The swap reverses straight into the opposite trade. If REVERSAL re-entry")
        print("  is net-negative and fresh entry is not, the swap's re-entry is the leak --")
        print("  a different question from spacing/cooldown, which was ruled out 2026-09-03.")

        # what the losers look like, for the entry-validation question
        if losers:
            print("\n  THE LOSERS IN NUMBERS")
            def med(rs, key):
                return statistics.median([r[key] for r in rs]) if rs else float("nan")
            print(f"    median best profit they ever showed : ${med(losers, 'mfe'):.2f}"
                  + (f"   (winners: ${med(winners, 'mfe'):.2f})" if winners else ""))
            print(f"    median worst move against them      : ${med(losers, 'mae'):.2f}"
                  + (f"   (winners: ${med(winners, 'mae'):.2f})" if winners else ""))
            print(f"    median minutes alive                : {med(losers, 'minutes'):.0f}"
                  + (f"   (winners: {med(winners, 'minutes'):.0f})" if winners else ""))
            for label, key in (("entry gap |close-EMA13|", "gap"),):
                lv = [r[key] for r in losers if isinstance(r[key], (int, float))]
                wv = [r[key] for r in winners if isinstance(r[key], (int, float))]
                if lv and wv:
                    print(f"    median {label:<28}: losers ${statistics.median(lv):.2f}  "
                          f"winners ${statistics.median(wv):.2f}")
            for d in ("BUY", "SELL"):
                dr = [r for r in rows if r["dir"] == d]
                if dr:
                    dw = [r for r in dr if r["profit"] > 0]
                    print(f"    {d:<5} {len(dr):>3} trades  {len(dw):>3} won  "
                          f"{money(sum(r['profit'] for r in dr)):>10}")

        if args.detail and losers:
            print("\n  EVERY LOSER")
            for r in sorted(losers, key=lambda r: r["profit"]):
                print(f"    {r['entry_utc'].astimezone(COLOMBO):%d %b %H:%M} SL  {r['dir']:<4} "
                      f"{r['entry_px']:>9.2f}  {money(r['profit']):>9}  "
                      f"best +${r['mfe']:>5.2f} worst -${r['mae']:>5.2f}  "
                      f"{r['minutes']:>4.0f}min  {r['bucket']}")

    print(f"\n{'=' * 94}")
    print("Buckets come from candle highs/lows, which show how far price went but not the")
    print("order of moves inside one candle. The candle a trade exited in is left out.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
