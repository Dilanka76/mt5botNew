"""What did changing trades by hand do? Take-profits moved, trades closed.

User, 2026-09-22: "we changed the TP manually yesterday while the bot was
running -- can you check the effect? If I had not changed it, would we
have won?"

Why this is answerable: the engine never enforces its own target in
software (on_tick only watches for the broker to close the position), so
a take-profit moved in MT5 is fully real and the bot never restores it.
But the bot WRITES the target it set on every trade_entered line, and the
broker records where each trade actually closed. Put side by side, every
manual change shows up.

For each trade it finds one of:

  UNTOUCHED        closed exactly as the bot set it up
  TP MOVED         closed at a take-profit that is not the bot's
  TP BYPASSED      the bot's original target was reached while the trade
                   was open, yet it did not close there -- so the target
                   had been moved further away or removed
  CLOSED BY HAND   from the phone, the desktop or the web

and for every changed trade, WHAT WOULD HAVE HAPPENED UNTOUCHED, by
replaying the real candles under the bot's own rules: the original
take-profit, the exit on the first candle that closes with the EMAs
against the position, and the broker backstop. Real lot size and the
trade's real costs throughout.

LIMITS, printed with the result: candles show how far price went, not in
what order inside one candle, and a replay after a hand-close starts from
the candle the close happened in. Treat single trades as estimates and
the total as the answer.

    python scripts/manual_change_check.py
    python scripts/manual_change_check.py --since "2026-09-16 00:00:00"

Read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

COLOMBO = ZoneInfo("Asia/Colombo")
OZ_PER_LOT = 100.0
TP_MATCH = 0.30          # $ -- a TP fill this close to the bot's target is the bot's
# A take-profit is sent as a market order when touched, so in a fast move it
# can fill PAST the target. The first run (2026-09-22) called five fills
# $0.33-$0.61 past target "TP MOVED" -- that is slippage, not a hand. A move
# by hand is deliberate and lands well away from the bot's level.
SLIPPAGE = 1.00
REACHED_BY = 0.25        # $ -- beyond the target by this much, clear of spread noise
MAX_REPLAY = timedelta(hours=48)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="live2_m3,live2_m5")
    p.add_argument("--since", default="2026-09-21 00:00:00", help="true UTC")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def read_entries(account: str) -> list:
    """Every trade_entered line, INCLUDING the rotated files. decisions.jsonl
    rolls over at 5 MB into decisions.jsonl.1 ... .5, so reading only the
    current file lost the older entries (15 of 59 live2_m3 trades showed
    'no entry record' on the first run)."""
    base = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    files = [base.with_name(f"decisions.jsonl.{i}") for i in range(5, 0, -1)] + [base]
    lines: list[str] = []
    for f in files:
        if f.is_file():
            lines.extend(f.read_text(errors="ignore").splitlines())
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "trade_entered" or e.get("tp") in (None, 0, 0.0):
            continue
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        out.append({"ts": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                    "direction": e.get("direction"), "entry": e.get("entry"),
                    "tp": float(e["tp"])})
    return out


def match(entries: list, direction: str, entry_price: float, when: datetime):
    """The logged entry for this trade: same side, same moment, about the
    same price. The log records the price the bot ASKED for (the tick at
    request), not the fill, so slippage moves them apart -- a 5-cent match
    lost every slipped entry on the first run."""
    best, best_d = None, 120.0
    for e in entries:
        if e["direction"] != direction or e["entry"] is None:
            continue
        if abs(float(e["entry"]) - entry_price) > 1.50:
            continue
        d = abs((e["ts"] - when).total_seconds())
        if d <= best_d:
            best, best_d = e, d
    return best


def who_closed(label: str) -> str:
    if label == "Take Profit":
        return "tp"
    if label == "Stop Loss":
        return "stop"
    if label == "EMA Cross Exit":
        return "bot"
    m = re.search(r"reason=(\d+)", label or "")
    if m and m.group(1) in ("0", "1", "2"):
        return "hand"
    return "bot"


def replay(df, bar, sign, entry, tp, backstop, start):
    """The bot's own exit, from `start`: original take-profit, the first
    candle closing with the EMAs against the position, or the backstop.
    Returns (exit_price, how)."""
    window = df[(df.index > start - bar) & (df.index <= start + MAX_REPLAY)]
    for t, row in window.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if backstop:
            stop = entry - sign * backstop
            if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
                return stop, "backstop"
        if (sign > 0 and hi >= tp) or (sign < 0 and lo <= tp):
            return tp, "take-profit"
        against = (row["ema13"] < row["ema21"]) if sign > 0 else (row["ema13"] > row["ema21"])
        if against and t + bar > start:
            return float(row["close"]), "opposite cross"
    return None, "still open at the end of the data"


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("MANUAL CHANGES -- take-profits moved and trades closed by hand, and what untouched would have done")
    print(f"since {since:%Y-%m-%d %H:%M} UTC")
    print("=" * 100)

    grand_actual = grand_untouched = 0.0
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
        df = compute_emas(df, config.ema_periods)
        bar = timedelta(minutes=minutes_for(config.timeframe))
        entries = read_entries(account)
        backstop = getattr(config, "broker_backstop_usd", None)

        print(f"\n{account}   {config.timeframe}")
        counts = {"UNTOUCHED": 0, "TP MOVED": 0, "TP BYPASSED": 0, "CLOSED BY HAND": 0, "NO RECORD": 0}
        acct_actual = acct_untouched = 0.0
        for t in sorted(raw, key=lambda x: x["entry_time"]):
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            entry, exit_px = float(t["entry_price"]), float(t["exit_price"])
            vol, profit = float(t["volume"]), float(t["profit"])
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            cost = profit - sign * (exit_px - entry) * vol * OZ_PER_LOT
            how = who_closed(t["exit_reason"])
            logged = match(entries, t["direction"], entry, entry_utc)
            if logged is None:
                counts["NO RECORD"] += 1
                continue
            tp = logged["tp"]

            # was the bot's own target reached BEFORE the trade really closed?
            firm = df[(df.index > entry_utc - bar) & (df.index + bar <= exit_utc)]
            reached = (not firm.empty and
                       ((sign > 0 and float(firm["high"].max()) >= tp + REACHED_BY) or
                        (sign < 0 and float(firm["low"].min()) <= tp - REACHED_BY)))

            past = sign * (exit_px - tp)          # + = filled beyond the target
            if how == "tp" and (abs(exit_px - tp) <= TP_MATCH or 0 <= past <= SLIPPAGE):
                kind = "UNTOUCHED"
            elif how == "hand":
                kind = "CLOSED BY HAND"
            elif how == "tp":
                kind = "TP MOVED"
            elif reached:
                kind = "TP BYPASSED"
            else:
                kind = "UNTOUCHED"
            counts[kind] += 1
            if kind == "UNTOUCHED":
                continue

            if reached:
                alt_px, alt_how = tp, "take-profit (reached while the trade was open)"
            else:
                alt_px, alt_how = replay(df, bar, sign, entry, tp, backstop, exit_utc)
            if alt_px is None:
                print(f"    {t['entry_time']:%d %b %H:%M} {t['direction']:<4} {kind}: cannot tell yet ({alt_how})")
                continue
            alt = sign * (alt_px - entry) * vol * OZ_PER_LOT + cost
            acct_actual += profit
            acct_untouched += alt
            print(f"    {t['entry_time']:%d %b %H:%M} SL  {t['direction']:<4} {vol:g} lots  "
                  f"in {entry:.2f}  bot's TP {tp:.2f}")
            print(f"        {kind:<15} really: out {exit_px:.2f}  {money(profit):>9}")
            print(f"        {'if untouched':<15} would: out {alt_px:.2f}  {money(alt):>9}  by {alt_how}"
                  f"   -> your change {money(profit - alt)}")

        print(f"  {counts['UNTOUCHED']} untouched, {counts['TP MOVED']} TP moved, "
              f"{counts['TP BYPASSED']} TP bypassed, {counts['CLOSED BY HAND']} closed by hand"
              + (f", {counts['NO RECORD']} with no entry record" if counts["NO RECORD"] else ""))
        if acct_actual or acct_untouched:
            print(f"  changed trades: really {money(acct_actual)}, untouched {money(acct_untouched)}"
                  f"  ->  the changes {'EARNED' if acct_actual >= acct_untouched else 'COST'} "
                  f"${abs(acct_actual - acct_untouched):,.2f}")
        grand_actual += acct_actual
        grand_untouched += acct_untouched

    print(f"\n{'=' * 100}")
    diff = grand_actual - grand_untouched
    print(f"ALL CHANGED TRADES: really {money(grand_actual)}, left to the bot {money(grand_untouched)}")
    print(f"  -> the manual changes {'EARNED' if diff >= 0 else 'COST'} ${abs(diff):,.2f} overall")
    print("\nCandles show how far price went, not the order inside one candle; single trades are")
    print("estimates, the total is the answer. A replay after a hand-close starts from its candle.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
