"""When a trade reaches take-profit, what if we did NOT close it -- but
moved the stop up to protect the profit and let it keep running?

User's proposal, 2026-09-07: "when it comes to the $5 TP, how can we keep
the trade and grab more profit, but every time we need to survive."

The two existing exits are unchanged in every variant below:
  - an opposite confirmed EMA13/21 cross still closes the trade;
  - the stop still closes it -- it is only MOVED, never removed.

What changes is that on reaching TP the broker take-profit is cancelled,
the stop jumps to a locked-in profit level, and the trade continues.
Worst case then becomes a WIN of the locked amount rather than the flat
TP -- the "survive" requirement, enforced structurally.

Variants tested, all distances in PRICE dollars (take_profit_usd and
stop_loss_usd are price distances -- $5 x 0.12 lots x 100 = $60):
  - "lock only": stop parks at +lock and never moves; ride until the
    opposite cross or the stop.
  - "lock + trail G": stop starts at +lock, then follows the best price
    seen at a distance of G behind it, ratcheting up only.

WHY THIS IS WORTH RE-TESTING even though trailing stops were ruled out
on 2026-09-03 (see project_post_exit_movement_null): that study measured
AVERAGE post-exit drift and found it symmetric -- favorable and adverse
continuation both scaling as sqrt(t), the signature of a random walk. A
trailing rule's value, though, does not come from average drift; it
comes from the SHAPE of the distribution (a few large runners paying for
many small give-backs). That is a different question, and it has never
been simulated trade by trade on this project's real data. Note the
theory is still against it: under a driftless random walk any stopping
rule has the same expected value as closing now, minus costs. This tests
whether reality departs from that.

METHOD, and its one real weakness: replays REAL candles after each TP
exit. Within a single candle the true order of the high and the low is
unknowable, so the ADVERSE extreme is always assumed to come first --
the stop is tested before the trail is raised. That is deliberately
pessimistic; it can understate a variant but never flatter it.

Trades still unresolved after --max-candles are EXCLUDED rather than
guessed at, and the count is reported.

    python scripts/simulate_tp_runner.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

USD_PER_LOT_PER_DOLLAR = 100.0  # XAUUSD


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--max-candles", type=int, default=300,
                   help="give up on a trade still open after this many candles")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker UTC offset; supply manually when the market is closed")
    return p.parse_args()


def tp_exit_tickets(account: str) -> set[int]:
    """Tickets the ENGINE recorded as closing at take-profit. Read from
    decisions.jsonl rather than inferred from P/L, so a trade that merely
    happened to end near TP is not mistaken for one."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out: set[int] = set()
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            action, ticket = e.get("action"), e.get("ticket")
            if ticket is None:
                continue
            if action == "trade_closed_tp" or (action == "trade_exited" and e.get("category") == "take_profit"):
                try:
                    out.add(int(ticket))
                except (TypeError, ValueError):
                    continue
    return out


def simulate(df: pd.DataFrame, start_after: datetime, direction: str, entry: float,
             lock: float, trail: float | None, max_candles: int,
             step: float | None = None) -> tuple[str, float] | None:
    """Replay candles after the TP moment. Returns (how_it_ended,
    profit_in_price_dollars), or None if still open at the horizon.

    Two ways of following price up, mutually exclusive:
      trail G : continuous -- the stop sits G behind the best price seen
                and is updated on every new high.
      step  S : stepped (the user's "$1 to $1" form) -- the stop jumps up
                in S-sized increments as the best price advances, so it
                moves a few times per trade instead of continuously. The
                gap therefore varies between 0 and S rather than being
                fixed. Fewer broker modify calls, which matters on a
                1-second polling loop.
    """
    future = df[df.index > start_after]
    if future.empty:
        return None
    is_buy = direction == "BUY"

    stop = lock          # profit level the stop protects, in price dollars
    best = lock          # best favourable excursion seen, in price dollars
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False  # shift(1) is NaN on row 0; it has no predecessor

    for i, (idx, row) in enumerate(future.iterrows()):
        if i >= max_candles:
            return None

        # Pessimistic ordering: the adverse extreme is assumed to happen
        # first, so the stop is tested before the trail can ratchet up.
        adverse = float(row["low"]) if is_buy else float(row["high"])
        adverse_profit = (adverse - entry) if is_buy else (entry - adverse)
        if adverse_profit <= stop:
            return ("stopped at lock" if stop <= lock else "trailed out", stop)

        favorable = float(row["high"]) if is_buy else float(row["low"])
        fav_profit = (favorable - entry) if is_buy else (entry - favorable)
        if fav_profit > best:
            best = fav_profit
            if trail is not None:
                stop = max(stop, best - trail)
            if step is not None and best > lock:
                stop = max(stop, lock + (int((best - lock) / step) * step))

        # The opposite-cross exit is unchanged from the live rule.
        if bool(changed.loc[idx]) and bool(above.loc[idx]) != is_buy:
            close_profit = (float(row["close"]) - entry) if is_buy else (entry - float(row["close"]))
            return ("opposite cross", close_profit)

    return None


def report(label: str, rows: list[dict], baseline_tp: float) -> None:
    if not rows:
        print(f"      {label}: no trades in this slice.")
        return
    base = sum(baseline_tp * r["volume"] * USD_PER_LOT_PER_DOLLAR for r in rows)
    got = sum(r["profit_price"] * r["volume"] * USD_PER_LOT_PER_DOLLAR for r in rows)
    diff = got - base
    print(f"      {label}: {len(rows)} trades  actual (flat TP) ${base:+.2f}  "
          f"-> rule ${got:+.2f}   {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        tp = config.take_profit_usd
        tickets = tp_exit_tickets(account)

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=1), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        winners = [t for t in trades if int(t["ticket"]) in tickets]
        print("=" * 84)
        print(f"{account} ({config.timeframe}): {len(winners)} take-profit exits since {args.since}  "
              f"(TP ${tp:.2f}, stop ${config.stop_loss_usd:.2f})")
        print("=" * 84)
        if not winners:
            print("  No take-profit exits in this window.\n")
            continue

        variants: list[tuple[str, float, float | None, float | None]] = [
            (f"lock ${tp - 0.50:.2f}, no trail        ", tp - 0.50, None, None),
            (f"lock ${tp - 0.50:.2f} + trail $0.50    ", tp - 0.50, 0.50, None),
            (f"lock ${tp - 0.50:.2f} + trail $1.00    ", tp - 0.50, 1.00, None),
            (f"lock ${tp:.2f} + trail $2.00    ", tp, 2.00, None),
            # The user's proposal, 2026-09-08: lock a FULL $1 below the
            # target (M1 $4, M3 $5) so a small pullback cannot stop the
            # trade out the instant it locks -- 6 of the first 7 real
            # locks did exactly that. Buys more runners at the price of a
            # $1 give-back on every trade that does not run, and unlike
            # lock-at-target it CAN finish worse than the old flat exit.
            (f"lock ${tp - 1.00:.2f} + trail $0.50    ", tp - 1.00, 0.50, None),
            (f"lock ${tp - 1.00:.2f} + trail $1.00    ", tp - 1.00, 1.00, None),
            (f"lock ${tp - 1.00:.2f} + trail $2.00    ", tp - 1.00, 2.00, None),
            # The user's stepped form, 2026-09-07: move the stop up $1 for
            # every $1 gained, then $2, then $3 -- fewer modify calls than
            # a continuous trail, and a wider effective gap.
            (f"lock ${tp - 0.50:.2f} + $1 steps      ", tp - 0.50, None, 1.00),
            (f"lock ${tp - 0.50:.2f} + $2 steps      ", tp - 0.50, None, 2.00),
            (f"lock ${tp - 0.50:.2f} + $3 steps      ", tp - 0.50, None, 3.00),
        ]

        for label, lock, trail, step in variants:
            rows, unresolved, endings = [], 0, {}
            for t in winners:
                out = simulate(df, t["exit_time"].astimezone(timezone.utc), t["direction"],
                               float(t["entry_price"]), lock, trail, args.max_candles, step)
                if out is None:
                    unresolved += 1
                    continue
                how, profit_price = out
                endings[how] = endings.get(how, 0) + 1
                rows.append({"time": t["entry_time"], "volume": float(t["volume"]),
                             "profit_price": profit_price})
            if not rows:
                print(f"  {label}: nothing resolved.\n")
                continue
            rows.sort(key=lambda r: r["time"])
            print(f"  {label}   ({unresolved} unresolved, excluded)")
            report("Full sample ", rows, tp)
            mid = len(rows) // 2
            report("First half  ", rows[:mid], tp)
            report("Second half ", rows[mid:], tp)
            print(f"      how they ended: " + ", ".join(f"{k}={v}" for k, v in sorted(endings.items())))
            print()

    print("Within-candle order is unknowable, so the adverse extreme is always assumed")
    print("first -- these numbers are pessimistic by construction, never flattering.")


if __name__ == "__main__":
    main()
