"""What a leg's RISK looks like at a real account size, not its profit.

Written 2026-09-13, the day before live2 is funded, in answer to:
*"now, you recommend we are going to live one today, mathematically tell me"*

Every fitting tool in this repo optimises and reports PROFIT, at a
$100,000 starting balance chosen so that lot size stays pinned to the top
tier and cannot masquerade as edge. That is the right choice for comparing
two candidate configurations against each other. It is the wrong number
entirely for deciding whether a $300 account survives, because:

  - at $100,000 the balance never leaves the top lot tier, so the
    simulation never de-risks the way a real small account does when it
    drops from $300 to $190 and steps down from 0.04 lots to 0.03;
  - total P/L says nothing about the equity path taken to reach it, and
    an account is closed by its worst moment, not by its final total.

So this script runs the config EXACTLY as deployed -- no swap_immediate
override, no level substitution, the real lot ladder -- from the real
starting balance, and reports only the things that can end an account:
per-trade risk, the daily P/L distribution, the worst day, the deepest
drawdown, the longest losing streak, and how often a daily loss limit
would actually have bound.

    python scripts/live_risk_profile.py --account demo1_m3 \\
        --from 2026-03-10 --to 2026-09-13 --balance 300 --offset-hours 3

Read-only: fetches candles once, then replays offline.
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import StaleTickError
from bot.backtest.runner import run_backtest
from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.adx import compute_adx
from bot.indicators.ema import compute_emas
from bot.indicators.htf_trend import compute_htf_trend
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots
from bot.timeframes import TIMEFRAME_MINUTES

# bot/daily_loss.py resets at Colombo midnight, so a "day" here must be the
# Colombo calendar day too. Grouping by UTC date would split the session --
# it runs 04:00-01:29 Colombo and wraps past midnight -- and would report a
# loss limit binding on days the real bot never saw as one day.
COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, type=validate_account_name)
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    p.add_argument("--balance", type=float, required=True,
                   help="the REAL starting balance, e.g. 300 -- not 100000")
    p.add_argument("--daily-loss-limit", type=float, default=None,
                   help="dollars; how often would this have bound? Defaults to the "
                        "config's own value if it has one")
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker-vs-UTC offset; pass 3 to run while the market is closed")
    return p.parse_args()


def as_utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    args = parse_args()
    config = load_config(args.account)
    setup_logging(config.logging, f"{args.account}-risk")

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    minutes = TIMEFRAME_MINUTES[config.timeframe]
    warmup = date_from - timedelta(minutes=config.candles_to_fetch * minutes)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                  else None)
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup, date_to, offset)
        htf_df = (get_ohlc_range(connector, config.symbol, config.htf_trend_timeframe,
                                 warmup, date_to, offset)
                  if config.htf_trend_timeframe is not None else None)
        info = connector.symbol_info(config.symbol)
        contract_size, point = info.trade_contract_size, info.point
    finally:
        connector.disconnect()

    df = compute_emas(df, config.ema_periods)
    if config.htf_trend_timeframe is not None:
        df = compute_htf_trend(df, htf_df, TIMEFRAME_MINUTES[config.htf_trend_timeframe],
                               config.ema_periods)
    if config.swap_adx_filter is not None:
        df = compute_adx(df, period=config.swap_adx_filter.adx_period)
    logging.getLogger("bot").setLevel(logging.WARNING)

    # The config EXACTLY as deployed. Nothing is overridden -- that is the
    # whole point of this script versus the fitting tools.
    trades = run_backtest(config, df, date_from, contract_size, point, args.balance)
    trades.sort(key=lambda t: as_utc(t["close_time"]))

    limit = args.daily_loss_limit
    if limit is None:
        limit = getattr(config, "daily_loss_limit_usd", None)

    print("=" * 84)
    print(f"RISK PROFILE — {args.account}, config exactly as deployed")
    print(f"range {args.date_from} .. {args.date_to} (UTC)")
    print(f"starting balance {money(args.balance)}   timeframe {config.timeframe}   "
          f"symbol {config.symbol}")
    print("=" * 84)

    if not trades:
        print("\nNo trades in this window — nothing to report.")
        return

    # ---- 1. what one trade can cost ---------------------------------
    lots = calculate_lots(args.balance, config.position_sizing)
    per_dollar = lots * contract_size          # account $ per $1 of price move
    print(f"\n1. RISK PER TRADE at {money(args.balance)}  (lot tier {lots})")
    print(f"   $1 of gold price          = {money(per_dollar)} of your account")
    if config.stop_loss_usd is not None:
        loss = config.stop_loss_usd * per_dollar
        print(f"   stop ${config.stop_loss_usd:.2f} hit          = {money(-loss)}"
              f"   ({loss / args.balance * 100:.1f}% of the account)")
    else:
        print("   stop                      : NONE (this leg holds to the opposite cross)")
    if config.take_profit_usd is not None:
        win = config.take_profit_usd * per_dollar
        print(f"   target ${config.take_profit_usd:.2f} hit        = {money(win)}"
              f"   ({win / args.balance * 100:.1f}% of the account)")
    backstop = getattr(config, "broker_backstop_usd", None)
    if backstop:
        bs = backstop * per_dollar
        print(f"   broker backstop ${backstop:.2f}    = {money(-bs)}"
              f"   ({bs / args.balance * 100:.1f}% — only if the software stop fails)")

    # ---- 2. the daily distribution ----------------------------------
    by_day: dict = defaultdict(float)
    for t in trades:
        by_day[as_utc(t["close_time"]).astimezone(COLOMBO).date()] += t["profit"]
    days = sorted(by_day)
    daily = [by_day[d] for d in days]
    mean, sd = statistics.mean(daily), (statistics.pstdev(daily) if len(daily) > 1 else 0.0)
    losing = [v for v in daily if v < 0]
    print(f"\n2. DAILY P/L over {len(days)} Colombo trading days ({len(trades)} trades, "
          f"{len(trades) / len(days):.1f}/day)")
    print(f"   average day               = {money(mean)}")
    print(f"   standard deviation        = {money(sd)}")
    print(f"   losing days               = {len(losing)} of {len(days)} "
          f"({len(losing) / len(days) * 100:.0f}%)")
    print(f"   worst day                 = {money(min(daily))}"
          f"   ({abs(min(daily)) / args.balance * 100:.1f}% of the starting account)")
    print(f"   best day                  = {money(max(daily))}")
    if sd > 0:
        print(f"   a 2-sigma bad day         = {money(mean - 2 * sd)}"
              f"   (happens roughly 1 day in 40)")

    # ---- 3. the equity path -----------------------------------------
    equity, peak, max_dd, dd_at, trough = args.balance, args.balance, 0.0, None, args.balance
    min_equity = args.balance
    for t in trades:
        equity += t["profit"]
        min_equity = min(min_equity, equity)
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > max_dd:
            max_dd, dd_at, trough = drop, as_utc(t["close_time"]).astimezone(COLOMBO).date(), equity
    print(f"\n3. EQUITY PATH")
    print(f"   final balance             = {money(equity)}")
    print(f"   lowest the account ever got = {money(min_equity)}")
    print(f"   deepest drawdown          = {money(-max_dd)} "
          f"({max_dd / peak * 100:.1f}% from its peak), bottoming {dd_at} at {money(trough)}")
    if min_equity <= 0:
        print("   *** THE ACCOUNT WENT TO ZERO. This configuration does not survive "
              "this window at this balance. ***")

    # ---- 4. losing streaks ------------------------------------------
    streak = worst_streak = 0
    streak_loss = worst_streak_loss = 0.0
    for t in trades:
        if t["profit"] < 0:
            streak += 1
            streak_loss += t["profit"]
            if streak > worst_streak:
                worst_streak, worst_streak_loss = streak, streak_loss
        else:
            streak, streak_loss = 0, 0.0
    print(f"\n4. STREAKS")
    print(f"   longest run of losses     = {worst_streak} trades, "
          f"{money(worst_streak_loss)} total")

    # ---- 5. would the daily limit have helped? ----------------------
    print(f"\n5. DAILY LOSS LIMIT")
    if limit is None:
        print("   not set on this config — nothing to evaluate")
    else:
        bound = [v for v in daily if v <= -limit]
        print(f"   limit                     = {money(limit)} "
              f"({limit / args.balance * 100:.0f}% of the starting account)")
        print(f"   days it would have bound  = {len(bound)} of {len(days)}")
        if not bound:
            print("   It never bound. On this history it is decoration, not protection —")
            print("   every real losing day finished well inside it.")
        print(f"   worst day was {money(min(daily))}, so the limit "
              f"{'DID' if min(daily) <= -limit else 'did NOT'} cap it.")

    print("\n" + "=" * 84)
    print("Profit is not reported here on purpose. The fitting scripts already")
    print("optimise it; nothing optimises survival, which is what this measures.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED — re-run with the broker's known offset:")
        print("\n    ... --offset-hours 3          (this broker runs UTC+3)\n")
        print(f"  {exc}")
        raise SystemExit(1)
