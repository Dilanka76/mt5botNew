"""Would a breakeven rule have helped? Replays every real trade's actual
price path under the rule and compares against what really happened.

Motivation (2026-09-04, see project_trade_protection_findings): the M3
accounts have NO breakeven protection at all -- breakeven_trigger_usd is
M1-only -- and MFE/MAE analysis showed 40% of demo1_m3's losing trades
were up $3 or more before dying, every one running on toward the $10
stop. Separately, M1's existing $4.50 trigger looks nearly inert (only
1% of losers ever reached +$5).

METHOD (agreed with the user before building):
  1. start at the real entry price
  2. walk forward through each candle the trade was open, in order
  3. track best profit reached (candle high for BUY, low for SELL)
  4. when best profit reaches the trigger, ARM: stop moves to entry+/-lock
  5. from the NEXT candle onward, if price returns to that level the
     trade closes there instead of its real outcome
  6. otherwise the trade keeps its real result

DELIBERATE CONSERVATISM -- within one candle the high/low ordering is
unknown, so:
  - arming only takes effect from the NEXT candle (mirrors how the real
    engine arms breakeven from completed candles)
  - if a candle contains BOTH the take-profit and the breakeven level,
    the breakeven is assumed to fire (the worse outcome for the rule)
Both choices UNDERSTATE the rule's benefit. If results look good even
understated, they can be trusted.

A trade that genuinely reached take-profit on an EARLIER candle is left
alone -- it had already closed.

    python scripts/simulate_breakeven_rule.py --accounts demo1_m3,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

TRIGGERS = [2.0, 2.5, 3.0, 3.5, 4.0]
LOCK = 0.5  # dollars of profit locked in, matching breakeven_lock_usd on M1
USD_PER_LOT_PER_DOLLAR = 100.0  # XAUUSD


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m3,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def simulate_one(trade: dict, window, trigger: float, take_profit: float) -> tuple[float, str]:
    """Returns (simulated_profit, outcome) where outcome is one of
    'unchanged', 'loser_saved', 'winner_cut'."""
    entry = float(trade["entry_price"])
    is_buy = trade["direction"] == "BUY"
    usd = float(trade["volume"]) * USD_PER_LOT_PER_DOLLAR
    real = float(trade["profit"])

    be_level = entry + LOCK if is_buy else entry - LOCK
    tp_level = entry + take_profit if is_buy else entry - take_profit

    armed = False
    for _, c in window.iterrows():
        high, low = float(c["high"]), float(c["low"])
        favourable = (high - entry) if is_buy else (entry - low)
        adverse_reached_be = (low <= be_level) if is_buy else (high >= be_level)
        tp_hit = (high >= tp_level) if is_buy else (low <= tp_level)

        if armed:
            # Breakeven is live from this candle onward.
            if adverse_reached_be:
                sim = LOCK * usd
                return sim, ("winner_cut" if real > sim else "loser_saved")
            if tp_hit:
                return real, "unchanged"   # ran to target before coming back
        else:
            if tp_hit:
                return real, "unchanged"   # already closed at target before arming
            if favourable >= trigger:
                armed = True               # takes effect from the NEXT candle

    return real, "unchanged"


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=1), now)
        finally:
            connector.disconnect()

        trades = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            window = df[(df.index >= entry_utc) & (df.index <= exit_utc)]
            if window.empty:
                continue
            trades.append((t, window, entry_utc))
        if not trades:
            print(f"{account}: no matched trades.\n")
            continue
        trades.sort(key=lambda x: x[2])

        real_total = sum(float(t["profit"]) for t, _, _ in trades)
        existing = config.breakeven_trigger_usd
        print(f"{'=' * 78}\n{account}: {len(trades)} trades, real P/L ${real_total:+.2f}   "
              f"(stop ${config.stop_loss_usd:.2f}, target ${config.take_profit_usd:.2f}, "
              f"breakeven now: {'$%.2f' % existing if existing else 'NONE'})\n{'=' * 78}")

        mid = len(trades) // 2
        for trigger in TRIGGERS:
            results = [simulate_one(t, w, trigger, config.take_profit_usd) for t, w, _ in trades]
            sim_total = sum(r[0] for r in results)
            saved = sum(1 for r in results if r[1] == "loser_saved")
            cut = sum(1 for r in results if r[1] == "winner_cut")
            diff = sim_total - real_total

            # Sanity: every changed trade must close at exactly the lock value.
            changed = [r[0] for r in results if r[1] != "unchanged"]
            bad = [v for v in changed if v <= 0]
            flag = ""
            if bad:
                flag = f"   *** WARNING: {len(bad)} 'saved' trades closed at <= $0 -- DO NOT TRUST ***"

            first = sum(r[0] for r in results[:mid]) - sum(float(t["profit"]) for t, _, _ in trades[:mid])
            second = sum(r[0] for r in results[mid:]) - sum(float(t["profit"]) for t, _, _ in trades[mid:])

            print(f"  trigger +${trigger:.2f} (lock ${LOCK:.2f}): P/L ${sim_total:+.2f} vs real ${real_total:+.2f} "
                  f"-> {'ADDED' if diff > 0 else 'COST'} ${abs(diff):.2f}{flag}")
            print(f"      {saved} losers saved, {cut} winners cut short   |   "
                  f"walk-forward: first half ${first:+.2f}, second half ${second:+.2f}")
        print()


if __name__ == "__main__":
    main()
