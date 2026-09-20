"""What if the take-profit were SMALLER? Replayed on real trades.

User's idea, 2026-09-20: keep the M15 trend rule but shrink both targets --
M3 $6 with the trend / $4 against (today $8 / $6), M5 $8 / $6 (today
$10 / $8). "How can we win like above about this 78 trades."

Method, per real trade: replay its own candles and find the best profit it
ever showed (MFE, in $/oz from the candle the trade entered on). Then, for
a target T:

    MFE >= T   the take-profit would have filled -> win of T,
               using the trade's REAL lot size and its REAL costs
               (recovered exactly: cost = profit - price move x lots x 100)
    MFE <  T   nothing changes: the trade ends exactly as it really did

ONLY SMALLER TARGETS CAN BE TESTED, and the script refuses larger ones.
A trade that hit its take-profit stopped there; the candles after it
belong to no trade, so there is no honest way to ask whether a BIGGER
target would have filled. Anyone who answers that from this data is
reading the future.

TWO HONEST LIMITS on the numbers below:

  SEQUENCE. A smaller target closes some trades earlier. The bot would
  then have been flat where it really was in a position, so some later
  reversal re-entries would never have happened. Entries here are held
  fixed at what really occurred -- this measures the target change alone,
  not the whole different history that would have followed.

  CANDLE ORDER. A candle shows how far price went, not in which order.
  Where a small target and the real exit fall in the same candle, the fill
  is assumed -- which flatters small targets slightly.

    python scripts/tp_what_if.py --since "2026-09-16 00:00:00" --offset-hours 3
    python scripts/tp_what_if.py --since "2026-09-09 00:00:00" --accounts demo2_m3,demo2_m5

Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

OZ_PER_LOT = 100.0

# (with the M15 trend, against it). The user's proposal is marked.
GRIDS = {
    "M3": [(8.0, 6.0), (7.0, 5.0), (6.0, 6.0), (6.0, 4.0), (5.0, 4.0), (4.0, 4.0), (5.0, 3.0)],
    "M5": [(10.0, 8.0), (9.0, 7.0), (8.0, 8.0), (8.0, 6.0), (7.0, 5.0), (6.0, 6.0), (6.0, 4.0)],
}
PROPOSED = {"M3": (6.0, 4.0), "M5": (8.0, 6.0)}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="live2_m3,live2_m5,demo2_m3,demo2_m5")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def read_aligned(account: str) -> list:
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
                    "direction": e.get("direction"), "aligned": e.get("htf_aligned"),
                    "target": e.get("target_usd")})
    return out


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 96)
    print("WHAT IF THE TARGET WERE SMALLER?  real trades, real lots, real costs")
    print(f"since {since:%Y-%m-%d %H:%M} UTC")
    print("=" * 96)

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
                                since - timedelta(days=1), now, offset)
        finally:
            connector.disconnect()

        logged = read_aligned(account)
        bar = timedelta(minutes=minutes_for(config.timeframe))
        now_pair = (float(config.htf_trend_take_profit_usd or config.take_profit_usd),
                    float(config.take_profit_usd))

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            firm = df[(df.index > entry_utc - bar) & (df.index + bar <= exit_utc)]
            entry, exit_px = float(t["entry_price"]), float(t["exit_price"])
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            at_exit = sign * (exit_px - entry)
            if firm.empty:
                mfe = max(at_exit, 0.0)
            elif sign > 0:
                mfe = max(float(firm["high"].max()) - entry, at_exit, 0.0)
            else:
                mfe = max(entry - float(firm["low"].min()), at_exit, 0.0)
            vol = float(t["volume"])
            profit = float(t["profit"])
            # exact fees for this trade: what the price move did not explain
            cost = profit - at_exit * vol * OZ_PER_LOT
            match, best_d = None, 90.0
            for e in logged:
                if e["direction"] != t["direction"]:
                    continue
                d = abs((e["ts"] - entry_utc).total_seconds())
                if d <= best_d:
                    match, best_d = e, d
            rows.append({"profit": profit, "vol": vol, "mfe": mfe, "cost": cost,
                         "aligned": bool(match and match["aligned"]),
                         "matched": match is not None})

        print(f"\n{'=' * 96}")
        print(f"{account}   {config.timeframe}   today: "
              f"${now_pair[0]:.2f} with the M15 trend / ${now_pair[1]:.2f} against")
        print("=" * 96)
        if not rows:
            print("  no trades")
            continue
        actual = sum(r["profit"] for r in rows)
        wins = sum(1 for r in rows if r["profit"] > 0)
        unmatched = sum(1 for r in rows if not r["matched"])
        print(f"  really happened: {len(rows)} trades, {wins} won "
              f"({100 * wins / len(rows):.0f}%), net {money(actual)}")
        if unmatched:
            print(f"  ({unmatched} trade(s) had no entry record; treated as against-trend)")

        print(f"\n  {'with':>6} {'against':>8} {'wins':>6} {'rate':>6} {'net':>12} "
              f"{'vs today':>11} {'rescued':>8} {'trimmed':>11}")
        results = []
        # Compare every row against the SIMULATED today row, never against
        # what really happened. The simulation caps a swap-exit winner that
        # ran past its target back to the target, so its own baseline sits
        # below reality -- measuring the other rows against reality would
        # charge them for that gap as well as for the target change.
        baseline = None
        for with_t, against_t in GRIDS.get(config.timeframe, []):
            if with_t > now_pair[0] + 0.001 or against_t > now_pair[1] + 0.001:
                continue        # bigger targets cannot be tested honestly
            net = rescued = trimmed = 0.0
            n_rescued = n_wins = 0
            for r in rows:
                target = with_t if r["aligned"] else against_t
                if r["mfe"] >= target:
                    pl = target * r["vol"] * OZ_PER_LOT + r["cost"]
                    n_wins += 1
                    if r["profit"] <= 0:
                        n_rescued += 1
                        rescued += pl - r["profit"]
                    else:
                        trimmed += pl - r["profit"]
                else:
                    pl = r["profit"]
                    if pl > 0:
                        n_wins += 1
                net += pl
            if (with_t, against_t) == now_pair:
                baseline = net
            results.append((with_t, against_t, net))
            mark = "  <- your idea" if PROPOSED.get(config.timeframe) == (with_t, against_t) else ""
            mark += "  (today)" if (with_t, against_t) == now_pair else ""
            print(f"  ${with_t:>5.2f} ${against_t:>7.2f} {n_wins:>6} "
                  f"{100 * n_wins / len(rows):>5.0f}% {money(net):>12} "
                  f"{money(net - baseline) if baseline is not None else '-':>11} "
                  f"{n_rescued:>4} {money(rescued):>11}"
                  f" {money(trimmed):>10}{mark}")
        print("  rescued = losers that would have hit the smaller target instead")
        print("  trimmed = what the winners give up by taking less")
        if baseline is not None:
            print(f"\n  the today row simulates {money(baseline)} against the {money(actual)} that")
            print("  really happened -- that gap is the simulation's own error (swap exits that")
            print("  ran PAST the target are capped back to it), so every row is compared to it.")
        if results:
            best = max(results, key=lambda r: r[2])
            print(f"  best on THIS data: ${best[0]:.2f} / ${best[1]:.2f} -> {money(best[2])}"
                  + (f" ({money(best[2] - baseline)} vs today)" if baseline is not None else ""))
            print("  On one stretch of days that is a suggestion, not a decision: the same")
            print("  table on demo2's longer history, split in halves, is what settles it.")

    print(f"\n{'=' * 96}")
    print("Entries are held fixed. A smaller target would have closed some trades earlier,")
    print("so some later reversal re-entries would never have happened -- that whole")
    print("alternative history is NOT simulated here, only the target change itself.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
