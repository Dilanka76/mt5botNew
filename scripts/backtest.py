"""Replays historical price data through the live strategy logic to
estimate its real edge over a much larger sample than the live/demo
accounts have accumulated so far.

    python scripts/backtest.py --account demo1 --from 2026-02-01 --to 2026-08-01

Writes reports/backtest/<account>/<from>_<to>.md (summary + methodology),
a sibling trades.jsonl (every simulated trade, raw), and a sibling .json
(the same summary/session/daily numbers as the markdown, machine-readable
— this is what the mobile app's GET /apiconnect/{account}/backtest
endpoint serves; see api_server.py). See bot/backtest/runner.py's module
docstring for exactly what this approximates and how — read that before
trusting the numbers.

Connects to MT5 only to pull historical data and the symbol's contract
size/point/current balance, then disconnects before the (offline) replay
begins — the live/demo trading connections on this same terminal are
never touched.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.runner import run_backtest
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector
from bot.trade_stats import (
    compute_daily_breakdown,
    compute_day_stats,
    compute_hour_of_day_breakdown,
    compute_session_breakdown,
)

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC")
    parser.add_argument(
        "--balance", type=float, default=None,
        help="Starting simulated balance. Defaults to the account's current real balance.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    # Deliberately a distinct log identity from the live account, not
    # args.account — main.py --account <this account> is very likely
    # running concurrently and already holds logs/<account>/app.log and
    # decisions.jsonl open. Sharing that path here causes two independent
    # RotatingFileHandlers to fight over the same file: a backtest burns
    # through the rollover size threshold in seconds (months of replay
    # trades vs. a live account's real pace), and Windows refuses to
    # rename a file another process still has open, silently dropping
    # whichever log record was mid-write in either process. Neither the
    # backtest report (built entirely from the in-memory trades list
    # below, never from decisions.jsonl) nor real trade execution (never
    # gated on a log write) are affected either way, but the live
    # account's own audit trail can lose entries during the collision —
    # not worth risking for a log stream this script doesn't need to
    # share in the first place.
    setup_logging(config.logging, f"{args.account}-backtest")

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if date_to <= date_from:
        raise ValueError("--to must be after --from")

    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to)
        symbol_info = connector.symbol_info(config.symbol)
        contract_size = symbol_info.trade_contract_size
        point = symbol_info.point
        starting_balance = args.balance if args.balance is not None else connector.account_info().balance
    finally:
        connector.disconnect()  # replay itself is fully offline from here

    df = compute_emas(df, config.ema_periods)
    trades = run_backtest(config, df, date_from, contract_size, point, starting_balance)

    out_dir = PROJECT_ROOT / "reports" / "backtest" / args.account
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.date_from}_{args.date_to}"

    (out_dir / f"{stem}.trades.jsonl").write_text("\n".join(json.dumps(t) for t in trades) + ("\n" if trades else ""))

    summary = compute_day_stats(trades)
    daily = compute_daily_breakdown(trades, days=(date_to - date_from).days or 1, today=date_to.date())
    sessions = compute_session_breakdown(trades, config.sessions[config.strategy_variant])
    hourly = compute_hour_of_day_breakdown(trades)
    hourly_ranked = sorted(
        (h for h in hourly if h["total_trades"] > 0),
        key=lambda h: (h["win_rate"], h["total_trades"]),
        reverse=True,
    )
    final_balance = starting_balance + summary["total_pl"]

    report = f"""# Backtest report — {args.account}

**Range:** {args.date_from} to {args.date_to} (UTC) · **Strategy variant:** {config.strategy_variant} · **Symbol:** {config.symbol}

## Summary

| Metric | Value |
|---|---|
| Total trades | {summary['total_trades']} |
| Wins / Losses / Breakeven | {summary['wins']} / {summary['losses']} / {summary['breakeven']} |
| Win rate | {summary['win_rate']:.1f}% |
| Total P/L | {summary['total_pl']:+.2f} |
| Avg win / Avg loss | {summary['avg_win']:+.2f} / {summary['avg_loss']:+.2f} |
| Starting balance | {starting_balance:.2f} |
| Ending balance (simulated) | {final_balance:.2f} |

## By session window

| Session | Trades | Wins | Win rate | Total P/L |
|---|---|---|---|---|
""" + "\n".join(
        f"| {s['label']} | {s['total_trades']} | {s['wins']} | {s['win_rate']:.1f}% | {s['total_pl']:+.2f} |"
        for s in sessions
    ) + f"""

## By hour of day (best to worst)

Every hour that had at least one trade, ranked by win rate — trade count
is shown alongside so you can judge how reliable each row is; a single
trade in an hour will show 100% or 0% and shouldn't be read as a real
edge (see the note at the bottom).

| Hour | Trades | Wins | Win rate | Total P/L |
|---|---|---|---|---|
""" + "\n".join(
        f"| {h['hour']:02d}:00 | {h['total_trades']} | {h['wins']} | {h['win_rate']:.1f}% | {h['total_pl']:+.2f} |"
        for h in hourly_ranked
    ) + f"""

## By day

| Date | Trades | Wins | Win rate | Total P/L |
|---|---|---|---|---|
""" + "\n".join(
        f"| {d['date']} | {d['total_trades']} | {d['wins']} | {d['win_rate']:.1f}% | {d['total_pl']:+.2f} |"
        for d in daily if d["total_trades"] > 0
    ) + """

## Methodology and known limitations

This is an approximation, not ground truth — read results with that in mind:

- **No real historical tick data.** Each 1-minute candle's own high/low
  are used as two synthetic ticks (in candle-direction order) rather than
  a true tick-by-tick replay. If a single bar's range spans both a profit
  target and a stop-loss, the synthetic ordering determines the outcome —
  """ + (
        f"and this run's config had `stop_loss_usd: {config.stop_loss_usd}` "
        "set, so that risk is live here: a trade whose candle touched both "
        "levels could show up as either exit depending on the synthetic "
        "ordering heuristic, not a true replay of which one the market hit "
        "first."
        if config.stop_loss_usd is not None else
        "this run's config has no `stop_loss_usd` set, so the only "
        "tick-checked exit is the take-profit — this risk doesn't apply "
        "here."
    ) + """
- **Spread is synthesized** from each candle's own MT5-reported spread
  and the symbol's point size, not the real historical spread at that
  instant.
- **P/L excludes commission/swap** — real trades will differ from this
  estimate by those amounts (a small, consistent gap; not modeled here).
- Assumes MT5's candle timestamps are true UTC (consistent with how the
  rest of this codebase already treats MT5 epoch timestamps for live
  trades) — not independently re-verified for this specific run.
- **The by-hour ranking is noisy for low-trade-count hours** — an hour
  with only 1-2 trades can show 100% or 0% purely by chance, not because
  that hour is actually good or bad. Weight hours with a meaningfully
  larger trade count more heavily than ones near the top/bottom with only
  a handful.
"""

    (out_dir / f"{stem}.md").write_text(report)

    # Machine-readable sibling — same numbers as the markdown report above,
    # reused unmodified (not re-derived), so the two can never disagree.
    # This is what api_server.py's /backtest endpoint reads for the mobile
    # app; the app has no use for the markdown/prose form.
    summary_json = {
        "account": args.account,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "strategy_variant": config.strategy_variant,
        "symbol": config.symbol,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            **summary,
            "starting_balance": starting_balance,
            "final_balance": final_balance,
        },
        "session_breakdown": sessions,
        "hourly_breakdown": hourly,
        "daily_breakdown": [d for d in daily if d["total_trades"] > 0],
    }
    (out_dir / f"{stem}.json").write_text(json.dumps(summary_json, indent=2))

    print(report)
    print(f"\nWritten to {out_dir / f'{stem}.md'}, {out_dir / f'{stem}.json'} and {out_dir / f'{stem}.trades.jsonl'}")


if __name__ == "__main__":
    main()
