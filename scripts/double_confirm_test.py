"""Wait ONE more candle before entering: does a second confirmation pay?

User, 2026-09-23, listing ways to avoid consolidation: ADX, "double candle
confirmation", EMA200, support/resistance. Three of those are settled --
ADX cost $90 live and holds 20% of the monster losses; EMA50/100 were
harmful 8 times out of 8 forward, and EMA200 is the same idea slower;
support/resistance IS the range filter already running on demo2. The
double confirmation has never been tested here. This tests it.

THE RULE BEING TESTED, exactly:

  the cross confirms at a candle close, as today -- but instead of
  entering at the next candle's open, the bot WAITS one full candle. If
  EMA13/21 are still on the same side at that candle's close, it enters at
  the following open. If they are not, the trade never happens.

Why it might work: a false cross in chop usually dies inside one candle,
so those entries disappear. Why it might not: in a real trend the cross
holds and you simply pay a worse price -- the same trade-off that killed
the smaller targets and the stop loss.

HOW EACH DELAYED TRADE IS SCORED: replayed forward under the bot's own
exits from the later entry -- its own take-profit (recomputed from the new
entry, since the target is a distance), the first candle closing with the
EMAs against it, and the broker backstop where the account has one. Real
lot size, and the trade's real costs. Skipped trades count as zero.

HONEST LIMITS, printed with the result: candles show how far price went,
not the order inside one; and a trade that never happens would have left
the bot flat, so some later reversal re-entries would differ -- that whole
alternative history is not simulated.

THE BAR, set before the first run: the delay is worth building only if it
gains money in BOTH halves, on BOTH M3 accounts AND both M5 accounts.

    python scripts/double_confirm_test.py --since "2026-08-25 00:00:00"
    python scripts/double_confirm_test.py --offset-hours 3      (weekends)

Read-only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

OZ_PER_LOT = 100.0
MAX_REPLAY = timedelta(hours=48)


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


def logged_targets(account: str) -> list:
    """Each entry's own target, so a delayed trade keeps the target the bot
    would really have given it (the M15 trend decides $6 or $8)."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    files = [path.with_name(f"decisions.jsonl.{i}") for i in range(5, 0, -1)] + [path]
    out = []
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text(errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") != "trade_entered" or not e.get("target_usd"):
                continue
            try:
                ts = datetime.fromisoformat(e["timestamp"])
            except (KeyError, ValueError):
                continue
            out.append({"ts": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                        "direction": e.get("direction"), "target": float(e["target_usd"])})
    return out


def target_for(entries: list, direction: str, when: datetime, fallback: float) -> float:
    best, best_d = None, 120.0
    for e in entries:
        if e["direction"] != direction:
            continue
        d = abs((e["ts"] - when).total_seconds())
        if d <= best_d:
            best, best_d = e, d
    return best["target"] if best else fallback


def replay(df, bar, sign, entry_px, target, backstop, start):
    """The bot's own exit from `start`: take-profit at entry +/- target,
    the first candle closing with the EMAs against it, or the backstop."""
    tp = entry_px + sign * target
    stop = entry_px - sign * backstop if backstop else None
    for t, row in df[(df.index >= start) & (df.index <= start + MAX_REPLAY)].iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if stop is not None and ((sign > 0 and lo <= stop) or (sign < 0 and hi >= stop)):
            return stop, "backstop"
        if (sign > 0 and hi >= tp) or (sign < 0 and lo <= tp):
            return tp, "take-profit"
        against = (row["ema13"] < row["ema21"]) if sign > 0 else (row["ema13"] > row["ema21"])
        if against:
            return float(row["close"]), "opposite cross"
    return None, "still open"


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("WAIT ONE MORE CANDLE BEFORE ENTERING -- replayed on real trades")
    print(f"since {since:%Y-%m-%d %H:%M} UTC")
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
        df = compute_emas(df, config.ema_periods)
        bar = timedelta(minutes=minutes_for(config.timeframe))
        entries = logged_targets(account)
        backstop = getattr(config, "broker_backstop_usd", None)

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            vol, profit = float(t["volume"]), float(t["profit"])
            entry_px, exit_px = float(t["entry_price"]), float(t["exit_price"])
            cost = profit - sign * (exit_px - entry_px) * vol * OZ_PER_LOT

            # The bot enters ~2s into the candle AFTER the cross confirmed,
            # so the candle it entered on IS the one the new rule waits
            # through: judge at that candle's close, enter on the next open.
            entry_candle = df[df.index <= entry_utc]
            if entry_candle.empty:
                continue
            i = df.index.get_loc(entry_candle.index[-1])
            if i + 1 >= len(df):
                continue
            wait = df.iloc[i]                     # the candle we now wait through
            still = (float(wait["ema13"]) > float(wait["ema21"])) if sign > 0 else \
                    (float(wait["ema13"]) < float(wait["ema21"]))
            row = {"entry": entry_utc, "profit": profit, "vol": vol, "sign": sign,
                   "took": bool(still)}
            if not still:
                row["alt"] = 0.0                  # the cross died: no trade at all
            else:
                new_px = float(df.iloc[i + 1]["open"])
                target = target_for(entries, t["direction"], entry_utc, config.take_profit_usd)
                out_px, _ = replay(df, bar, sign, new_px, target, backstop, df.index[i + 1])
                row["alt"] = (sign * (out_px - new_px) * vol * OZ_PER_LOT + cost
                              if out_px is not None else profit)
            rows.append(row)

        if not rows:
            print(f"\n{account}: no trades")
            continue
        order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry"]))}
        half = len(rows) // 2
        real = sum(r["profit"] for r in rows)
        alt = sum(r["alt"] for r in rows)
        skipped = [r for r in rows if not r["took"]]
        d1 = sum(r["alt"] - r["profit"] for r in rows if order[id(r)] < half)
        d2 = sum(r["alt"] - r["profit"] for r in rows if order[id(r)] >= half)

        print(f"\n{account}   {config.timeframe}   {len(rows)} trades")
        print(f"    really            {money(real)}")
        print(f"    waiting 1 candle  {money(alt)}   ->  {money(alt - real)}"
              f"   halves {money(d1)} / {money(d2)}")
        print(f"    the wait cancels  {len(skipped)} of {len(rows)} entries "
              f"({100 * len(skipped) / len(rows):.0f}%), which were really "
              f"{money(sum(r['profit'] for r in skipped))}")
        kept = [r for r in rows if r["took"]]
        if kept:
            print(f"    the rest entered later: {money(sum(r['alt'] - r['profit'] for r in kept))} "
                  f"from the worse price")

    print(f"\n{'=' * 100}")
    print("THE BAR (set before this run): worth building only if it gains in BOTH halves, on")
    print("BOTH M3 accounts AND both M5 accounts. Candles cannot order moves inside one candle,")
    print("and a cancelled trade would have left the bot flat -- later re-entries would differ.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
