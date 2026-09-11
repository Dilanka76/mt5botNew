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

import numpy as np
import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.formatting import usd
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
    p.add_argument("--real-ticks", action="store_true",
                   help="replay every real TICK instead of candle high/low. Removes the "
                        "within-candle ordering guess entirely — one true number instead "
                        "of a pessimistic/optimistic range.")
    p.add_argument("--tick-window-minutes", type=int, default=120,
                   help="how long after the take-profit to follow each trade with ticks "
                        "(default 120; real runner exits land within minutes)")
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


def build_context(df: pd.DataFrame) -> dict:
    """Everything simulate() needs, extracted from the frame ONCE.

    simulate used to rebuild the EMA-cross state over the whole frame and
    boolean-mask the future on every call. For the 59-trade study that was
    invisible; scripts/fit_runner.py makes ~35,000 calls against 35,000
    candles and it became the whole runtime. Numpy arrays plus an integer
    start position make each call O(candles actually walked).
    """
    above = (df["ema13"] > df["ema21"]).to_numpy()
    changed = np.zeros(len(above), dtype=bool)
    changed[1:] = above[1:] != above[:-1]      # row 0 has no predecessor
    return {
        "high": df["high"].to_numpy(dtype=float),
        "low": df["low"].to_numpy(dtype=float),
        "close": df["close"].to_numpy(dtype=float),
        "above": above,
        "changed": changed,
    }


def simulate(df: pd.DataFrame, start_after: datetime, direction: str, entry: float,
             lock: float, trail: float | None, max_candles: int,
             step: float | None = None, ratchet_first: bool = False,
             ctx: dict | None = None, start_pos: int | None = None) -> tuple[str, float] | None:
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
    if ctx is None:
        ctx = build_context(df)
    if start_pos is None:
        start_pos = int(df.index.searchsorted(start_after, side="right")) - 1
    first = start_pos + 1
    if first >= len(ctx["high"]):
        return None
    is_buy = direction == "BUY"

    stop = lock          # profit level the stop protects, in price dollars
    best = lock          # best favourable excursion seen, in price dollars
    highs, lows, closes = ctx["high"], ctx["low"], ctx["close"]
    above, changed = ctx["above"], ctx["changed"]

    last = min(first + max_candles, len(highs))
    for pos in range(first, last):

        # A candle gives a high and a low but not their ORDER, and for a
        # trailing stop the order decides the outcome. Both ways are
        # simulated because each is pessimistic about a different thing.
        #
        # stop-first (the original): test the low against the CURRENT stop,
        #   then ratchet on the high. Pessimistic about the entry -- but
        #   optimistic about the trail, because a candle that makes a new
        #   high and THEN falls back past the raised stop survives here
        #   while it would be stopped out in real life. That bias grows as
        #   the trail shrinks relative to the candle: M3's median range is
        #   $3.56 against a $0.50 trail, so almost any candle making a new
        #   high also retraces $0.50 inside itself.
        #
        # ratchet-first: raise the stop on the high, then test the low
        #   against the RAISED stop. I first described this as "pessimistic
        #   about the trail". That was wrong, and the run on 59 real trades
        #   showed it: ratchet-first scores HIGHER everywhere, so it is the
        #   OPTIMISTIC bound, not a second pessimistic one.
        #
        #   Why: on a wide candle whose low is already below the stop,
        #   stop-first exits at that low stop, while ratchet-first has
        #   raised it to (high - trail) and banks near the candle's peak.
        #   That gain dwarfs the shake-out effect it also introduces.
        #
        # So the two are a PESSIMISTIC and an OPTIMISTIC bound -- adverse
        # extreme always first, versus favourable extreme always first --
        # and reality is a mix. Read them as a range, and trust only a
        # setting that wins at BOTH ends.
        adverse = lows[pos] if is_buy else highs[pos]
        adverse_profit = (adverse - entry) if is_buy else (entry - adverse)
        favorable = highs[pos] if is_buy else lows[pos]
        fav_profit = (favorable - entry) if is_buy else (entry - favorable)

        def ratchet() -> None:
            nonlocal best, stop
            if fav_profit > best:
                best = fav_profit
                if trail is not None:
                    stop = max(stop, best - trail)
                if step is not None and best > lock:
                    stop = max(stop, lock + (int((best - lock) / step) * step))

        if ratchet_first:
            ratchet()
            if adverse_profit <= stop:
                return ("stopped at lock" if stop <= lock else "trailed out", stop)
        else:
            if adverse_profit <= stop:
                return ("stopped at lock" if stop <= lock else "trailed out", stop)
            ratchet()

        # The opposite-cross exit is unchanged from the live rule.
        if changed[pos] and bool(above[pos]) != is_buy:
            close_profit = (closes[pos] - entry) if is_buy else (entry - closes[pos])
            return ("opposite cross", close_profit)

    return None


def fetch_ticks(symbol: str, date_from: datetime, date_to: datetime,
                offset: timedelta) -> pd.DataFrame:
    """Real ticks between two true-UTC times, as bid/ask indexed by time.

    mt5.copy_ticks_range() speaks MT5's broker-time convention, not true
    UTC -- the same trap as history_deals_get() and copy_rates_range().
    The window is shifted into that convention to query and the returned
    times shifted back, so callers only ever deal in true UTC. Lifted from
    scripts/backtest.py's _fetch_real_ticks, which already had this right.
    """
    import MetaTrader5 as mt5

    ticks = mt5.copy_ticks_range(symbol, date_from + offset, date_to + offset,
                                 mt5.COPY_TICKS_ALL)
    if ticks is None or len(ticks) == 0:
        return pd.DataFrame(columns=["bid", "ask"],
                            index=pd.DatetimeIndex([], tz=timezone.utc))
    times = pd.to_datetime(ticks["time_msc"], unit="ms", utc=True) - offset
    out = pd.DataFrame({"bid": ticks["bid"], "ask": ticks["ask"]}, index=times)
    out = out[out["bid"] > 0.0]      # some feeds emit zero-price keepalives
    return out.sort_index()


def simulate_on_ticks(ticks: pd.DataFrame, direction: str, entry: float, lock: float,
                      trail: float | None, step: float | None,
                      cross_at: datetime | None, cross_price: float | None
                      ) -> tuple[str, float] | None:
    """The runner replayed tick by tick — no ordering assumption at all.

    A candle gives a high and a low but not their order, which is the
    whole reason candle mode has to be run twice and read as a range.
    Ticks ARE the order, so this returns one true number.

    Each tick is checked against the stop first and only then allowed to
    ratchet it, which is not an assumption here but simply what a stop
    does: the price has to exist before the trail can follow it.
    """
    if ticks.empty:
        return None
    is_buy = direction == "BUY"
    stop = lock
    best = lock

    # A BUY exits at bid, a SELL at ask -- the same side the broker fills.
    prices = (ticks["bid"] if is_buy else ticks["ask"]).to_numpy(dtype=float)
    for price in prices:
        profit = (price - entry) if is_buy else (entry - price)
        if profit <= stop:
            return ("stopped at lock" if stop <= lock else "trailed out", stop)
        if profit > best:
            best = profit
            if trail is not None:
                stop = max(stop, best - trail)
            if step is not None and best > lock:
                stop = max(stop, lock + (int((best - lock) / step) * step))

    if cross_at is not None and cross_price is not None:
        return ("opposite cross", (cross_price - entry) if is_buy else (entry - cross_price))
    return None


def first_opposite_cross(df: pd.DataFrame, start_after: datetime,
                         is_buy: bool) -> tuple[datetime, float] | None:
    """When the live swap rule would have closed this trade, and at what
    price. Ticks cannot see an EMA cross, which is defined on candles."""
    above = df["ema13"] > df["ema21"]
    changed = above != above.shift(1)
    changed.iloc[0] = False
    future = df[df.index > start_after]
    for idx, row in future.iterrows():
        if bool(changed.loc[idx]) and bool(above.loc[idx]) != is_buy:
            return idx.to_pydatetime(), float(row["close"])
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
        tick_cache: dict[int, pd.DataFrame] = {}
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=1), now)

            if args.real_ticks:
                # Fetched once per trade and reused by every variant --
                # otherwise 24 variants would refetch the same ticks 24 times.
                wanted = [t for t in trades if int(t["ticket"]) in tp_exit_tickets(account)]
                print(f"  Fetching real ticks for {len(wanted)} trades "
                      f"({args.tick_window_minutes} min each) ...", flush=True)
                for t in wanted:
                    start = t["exit_time"].astimezone(timezone.utc)
                    tick_cache[int(t["ticket"])] = fetch_ticks(
                        config.symbol, start,
                        start + timedelta(minutes=args.tick_window_minutes), offset)
                got = sum(len(v) for v in tick_cache.values())
                empty = sum(1 for v in tick_cache.values() if v.empty)
                print(f"  {got:,} ticks. {empty} of {len(tick_cache)} trades have NO tick "
                      f"history{' (broker keeps less tick than candle history)' if empty else ''}.",
                      flush=True)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        winners = [t for t in trades if int(t["ticket"]) in tickets]
        print("=" * 84)
        print(f"{account} ({config.timeframe}): {len(winners)} take-profit exits since {args.since}  "
              f"(TP ${tp:.2f}, stop {usd(config.stop_loss_usd)})")
        print("=" * 84)
        if not winners:
            print("  No take-profit exits in this window.\n")
            continue

        variants: list[tuple[str, float, float | None, float | None]] = [
            (f"lock ${tp - 0.50:.2f}, no trail        ", tp - 0.50, None, None),
            (f"lock ${tp - 0.50:.2f} + trail $0.50    ", tp - 0.50, 0.50, None),
            (f"lock ${tp - 0.50:.2f} + trail $1.00    ", tp - 0.50, 1.00, None),
            (f"lock ${tp:.2f} + trail $2.00    ", tp, 2.00, None),
            # Never tested until 2026-09-08: lock AT the target (which M1
            # prefers -- it cannot afford the give-back of a lower lock)
            # paired with a TIGHT trail. The M3 result showed the tight
            # trail is what banks a run, and unlike a lower lock it costs
            # nothing on the trades that do not run. If it helps here,
            # M1 gets the benefit without paying for it.
            (f"lock ${tp:.2f} + trail $0.50    ", tp, 0.50, None),
            (f"lock ${tp:.2f} + trail $1.00    ", tp, 1.00, None),
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

        orderings = ["stop-first", "ratchet-first"] + (["REAL TICKS"] if args.real_ticks else [])
        for label_base, lock, trail, step in variants:
          for ordering in orderings:
            rows, unresolved, endings = [], 0, {}
            for t in winners:
                start = t["exit_time"].astimezone(timezone.utc)
                if ordering == "REAL TICKS":
                    cross = first_opposite_cross(df, start, t["direction"] == "BUY")
                    ticks = tick_cache.get(int(t["ticket"]))
                    if ticks is None or ticks.empty:
                        unresolved += 1
                        continue
                    if cross is not None:
                        ticks = ticks[ticks.index <= cross[0]]
                    out = simulate_on_ticks(ticks, t["direction"], float(t["entry_price"]),
                                            lock, trail, step,
                                            cross[0] if cross else None,
                                            cross[1] if cross else None)
                else:
                    out = simulate(df, start, t["direction"],
                                   float(t["entry_price"]), lock, trail, args.max_candles, step,
                                   ratchet_first=(ordering == "ratchet-first"))
                if out is None:
                    unresolved += 1
                    continue
                how, profit_price = out
                endings[how] = endings.get(how, 0) + 1
                rows.append({"time": t["entry_time"], "volume": float(t["volume"]),
                             "profit_price": profit_price})
            label = f"{label_base}  [{ordering:<13}]"
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

    print("Within-candle order is unknowable, so each variant is shown twice: stop-first\n"
          "assumes the adverse extreme came first, ratchet-first assumes the favourable one\n"
          "did. I called these a PESSIMISTIC and an OPTIMISTIC bound and said the truth lies\n"
          "between them. The REAL TICKS rows on 2026-09-11 came in BELOW BOTH, every time.\n"
          "\n"
          "Both orderings model one high and one low per candle. Real price oscillates many\n"
          "times inside a candle, and every swing bigger than the trail takes the stop out.\n"
          "So candle rows OVERSTATE a trailing stop, and the tighter the trail is against the\n"
          "candle range, the worse the overstatement: on M3 (median range $3.56) a $0.50\n"
          "trail showed +$968 / +$1,837 on candles and +$242 on ticks.\n"
          "\n"
          "Use the REAL TICKS row. The candle rows are only a fallback when tick history is\n"
          "unavailable, and they are not a bound.")


if __name__ == "__main__":
    main()
