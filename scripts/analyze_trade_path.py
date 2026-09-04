"""How far does each trade travel FOR and AGAINST us while it is open?

The second half of the user's core question (2026-09-04): "how can we
trust the entry, and how can we protect the trade after enter." Entry
quality has had days of work; the protection side has barely been
examined -- and there is an obvious hole: the M3 accounts have NO
breakeven protection at all (breakeven_trigger_usd is M1-only), so an M3
trade can show real profit, reverse, and run to the full $10 stop.

Standard trade-path analysis. For every closed trade, replays the real
candles between entry and exit and measures:

  MFE (max favourable excursion) -- the best unrealised profit the trade
      ever showed. On LOSING trades this is profit we gave back, and it
      is exactly what a breakeven rule would have protected.
  MAE (max adverse excursion) -- the worst unrealised loss the trade ever
      showed. On WINNING trades this is how close we came to being
      stopped out; it says whether the stop is too tight or has room.

Reported in dollars using each trade's own volume (XAUUSD: $100 per lot
per $1), so figures are directly comparable to real P/L, plus the two
questions that decide whether a breakeven rule is worth adding to M3:

  - what share of LOSERS were ever showing >= $2/$3/$4/$5 profit
  - what share of WINNERS ever dipped >= $2/$4/$6/$8 against us

Candle high/low is a conservative approximation of the intra-trade path
(the same approximation scripts/backtest.py uses); it cannot tell the
order in which the high and low occurred within one candle.

    python scripts/analyze_trade_path.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

USD_PER_LOT_PER_DOLLAR = 100.0  # XAUUSD
PROFIT_LEVELS = [2.0, 3.0, 4.0, 5.0]   # $ of unrealised profit, for the loser question
PAIN_LEVELS = [2.0, 4.0, 6.0, 8.0]     # $ of unrealised loss, for the winner question


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def share_at_least(values: list[float], level: float) -> str:
    if not values:
        return "n/a"
    hit = sum(1 for v in values if v >= level)
    return f"{hit}/{len(values)} ({100*hit/len(values):.0f}%)"


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

        # price-move MFE/MAE (in $ of price) and the same in account dollars
        win_mae_price, win_mfe_price, loss_mfe_price, loss_mae_price = [], [], [], []
        loss_mfe_usd, win_mae_usd = [], []
        skipped = 0

        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            # Window starts AT entry, never before. An earlier version used a
            # 5-minute "safety buffer" before entry_utc, which measured price
            # movement from before the trade existed and produced impossible
            # results -- winners showing $10 drawdowns against a $5 stop
            # (2026-09-04). Candles at or after entry_utc only.
            window = df[(df.index >= entry_utc) & (df.index <= exit_utc)]
            if window.empty:
                skipped += 1
                continue
            entry = float(t["entry_price"])
            if t["direction"] == "BUY":
                mfe = float(window["high"].max()) - entry
                mae = entry - float(window["low"].min())
            else:
                mfe = entry - float(window["low"].min())
                mae = float(window["high"].max()) - entry
            mfe = max(mfe, 0.0)
            mae = max(mae, 0.0)
            usd = float(t["volume"]) * USD_PER_LOT_PER_DOLLAR

            if t["profit"] > 0:
                win_mae_price.append(mae)
                win_mfe_price.append(mfe)
                win_mae_usd.append(mae * usd)
            else:
                loss_mfe_price.append(mfe)
                loss_mae_price.append(mae)
                loss_mfe_usd.append(mfe * usd)

        n_win, n_loss = len(win_mae_price), len(loss_mfe_price)
        if n_win + n_loss == 0:
            print(f"{account}: no matched trades.\n")
            continue

        be = config.breakeven_trigger_usd
        print(f"{'=' * 78}\n{account}: {n_win} winners, {n_loss} losers   "
              f"(stop ${config.stop_loss_usd:.2f}, target ${config.take_profit_usd:.2f}, "
              f"breakeven {'$%.2f' % be if be else 'NONE'})\n{'=' * 78}")

        if loss_mfe_price:
            print(f"  LOSING trades -- how much profit did they show before dying?")
            print(f"    median best profit reached: ${statistics.median(loss_mfe_price):.2f} of price "
                  f"(${statistics.median(loss_mfe_usd):+.2f} account)")
            for lvl in PROFIT_LEVELS:
                print(f"      ever reached +${lvl:.2f}: {share_at_least(loss_mfe_price, lvl)}")

        if win_mae_price:
            print(f"  WINNING trades -- how much pain did they take before winning?")
            print(f"    median worst drawdown: ${statistics.median(win_mae_price):.2f} of price "
                  f"(${statistics.median(win_mae_usd):.2f} account)")
            for lvl in PAIN_LEVELS:
                print(f"      ever dipped -${lvl:.2f}: {share_at_least(win_mae_price, lvl)}")
            worst = max(win_mae_price)
            print(f"    worst drawdown on a trade that still won: ${worst:.2f}")
            # Hard sanity check: a winning trade cannot have gone further
            # against us than its own stop distance -- it would have been
            # closed as a loss first. If this trips, the measurement is
            # wrong, not the market (this exact check caught a real bug on
            # 2026-09-04 where the window started before entry).
            impossible = [m for m in win_mae_price if m > config.stop_loss_usd + 0.5]
            if impossible:
                print(f"    *** WARNING: {len(impossible)} winners show a drawdown deeper than the "
                      f"${config.stop_loss_usd:.2f} stop (worst ${max(impossible):.2f}). "
                      f"That is impossible -- DO NOT TRUST THESE NUMBERS. ***")
        if skipped:
            print(f"  ({skipped} trades skipped -- no candle data in window)")
        print()


if __name__ == "__main__":
    main()
