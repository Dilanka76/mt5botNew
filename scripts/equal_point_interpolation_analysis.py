"""Backtest-only analysis: for real immediate-entry trades (from an
existing scripts/backtest.py run), computes where EMA13 and EMA21
would have mathematically crossed WITHIN the cross candle itself,
using linear interpolation between the previous candle's EMA13/21 and
the cross candle's EMA13/21 — then estimates the entry price at that
exact point, and what each trade's profit would have been if it had
entered there instead of at the real (close-based) fill.

NOT deployed anywhere, not part of the live engine or the current
backtest driver — this tests a proposed idea (act at the mathematical
"equal point" itself, mid-candle) that the user explicitly wants
measured before deciding whether to pursue it further. See the
conversation/plan context: this is understood to reopen the exact
false-signal risk the EMA5/EMA9 confirmation exists to guard against —
this script exists to put a real number on the tradeoff, not to argue
for or against it.

THE MATH (linear interpolation):
Treat the previous candle's close time as t=0 and the cross candle's
close time as t=1 (a 1-minute span, spanning exactly the cross
candle's own open-to-close duration). EMA13 and EMA21 are each
approximated as straight lines between their two known values (one per
candle — there is no real tick-level EMA, only ever one number per
candle). Solving for where those two lines intersect:

    f = (ema21_prev - ema13_prev) / ((ema13_curr - ema13_prev) - (ema21_curr - ema21_prev))

`f` is the fraction of the way through the cross candle where the
crossing mathematically happens (0 = candle's open, 1 = candle's
close). The same fraction is then applied to the candle's own
open->close price range (the only price path data available — no real
tick history) to estimate the price at that moment:

    interpolated_price = open_curr + f * (close_curr - open_curr)

CAVEAT, stated plainly: this script keeps every trade's ACTUAL recorded
exit price and reason unchanged, and only substitutes a different
entry price — it does NOT re-simulate whether a shifted take-profit
level would have been hit sooner, later, or not at all, or whether a
different entry timing changes which candle EMA5/EMA9 or a new cross
would have fired on. It's a first-order "what would this exact trade's
P/L have been with a different entry price" estimate, not a full
re-simulation (unlike breakeven_stop_analysis.py, which needed full
re-simulation for the same class of reason). Good enough to see the
scale of the effect; a full re-simulation would be the next step if
this looks promising.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import Direction
from bot.trade_stats import compute_day_stats

TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, UTC — must match an existing backtest run")
    return parser.parse_args()


def load_trades(account: str, date_from: str, date_to: str) -> list[dict]:
    path = PROJECT_ROOT / "reports" / "backtest" / account / f"{date_from}_{date_to}.trades.jsonl"
    if not path.exists():
        raise SystemExit(
            f"{path} not found — run scripts/backtest.py for this account/range first:\n"
            f"  python scripts/backtest.py --account {account} --from {date_from} --to {date_to}"
        )
    trades = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            trades.append(json.loads(line))
    return trades


def interpolate_crossing_fraction(ema13_prev: float, ema21_prev: float, ema13_curr: float, ema21_curr: float) -> float | None:
    """Returns f in [0, 1], or None if the lines don't actually cross
    within this interval under linear interpolation (shouldn't normally
    happen for a real detected cross, but real EMA movement isn't
    perfectly linear — guard against it rather than trust blindly)."""
    denom = (ema13_curr - ema13_prev) - (ema21_curr - ema21_prev)
    if denom == 0:
        return None
    f = (ema21_prev - ema13_prev) / denom
    if f < 0.0 or f > 1.0:
        return None
    return f


def interpolated_price(open_curr: float, close_curr: float, f: float) -> float:
    return open_curr + f * (close_curr - open_curr)


def main() -> None:
    args = parse_args()
    trades = load_trades(args.account, args.date_from, args.date_to)
    immediate = [t for t in trades if t.get("entry_type") == "immediate"]
    print(f"Loaded {len(trades)} trades ({len(immediate)} immediate-entry — the only ones this idea applies to;")
    print("EMA5-touch entries already fill at a real live price, no candle-close delay to interpolate around).\n")

    config = load_config(args.account)
    date_from_dt = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    date_to_dt = datetime.strptime(args.date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = date_from_dt - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, date_to_dt)
        contract_size = connector.symbol_info(config.symbol).trade_contract_size
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    df = compute_emas(df, config.ema_periods)

    scored = []
    baseline = []  # same trades, unmodified — for a like-for-like comparison
    unscored = 0
    for t in immediate:
        cross_time = pd.Timestamp(t["open_time"])
        pos = df.index.searchsorted(cross_time)
        if pos <= 0 or pos >= len(df):
            unscored += 1
            continue
        cross_row = df.iloc[pos]
        prev_row = df.iloc[pos - 1]

        f = interpolate_crossing_fraction(
            float(prev_row["ema13"]), float(prev_row["ema21"]),
            float(cross_row["ema13"]), float(cross_row["ema21"]),
        )
        if f is None:
            unscored += 1
            continue

        interp_price = interpolated_price(float(cross_row["open"]), float(cross_row["close"]), f)
        original_entry = t["entry_price"]
        direction = Direction(t["direction"])
        sign = 1 if direction == Direction.BUY else -1

        # First-order re-estimate: same exit price/reason, different entry.
        exit_price = t["price"]
        new_profit = (exit_price - interp_price) * sign * t["volume"] * contract_size

        t2 = dict(t)
        t2["_interp_entry"] = interp_price
        t2["_interp_fraction"] = f
        t2["_entry_shift"] = interp_price - original_entry
        t2["profit"] = new_profit
        scored.append(t2)
        baseline.append(t)

    print(f"{len(scored)} of {len(immediate)} immediate trades matched to real candle data and interpolated ")
    print(f"({unscored} skipped — crossing fraction fell outside [0,1] under linear interpolation, or no matching candle).\n")

    if not scored:
        print("Nothing to report.")
        return

    shifts = [abs(t["_entry_shift"]) for t in scored]
    avg_shift = sum(shifts) / len(shifts)
    max_shift = max(shifts)
    print(f"Average |entry price shift| vs. the real (close-based) fill: ${avg_shift:.2f}")
    print(f"Largest single shift: ${max_shift:.2f}\n")

    old_summary = compute_day_stats(baseline)
    new_summary = compute_day_stats(scored)

    print("Immediate-entry trades ONLY (ema5_touch trades excluded from both sides, unaffected by this idea):\n")
    print(f"{'':22}{'REAL FILL':>14}{'INTERPOLATED':>16}")
    print(f"{'Total trades':22}{old_summary['total_trades']:>14}{new_summary['total_trades']:>16}")
    print(f"{'Wins':22}{old_summary['wins']:>14}{new_summary['wins']:>16}")
    print(f"{'Losses':22}{old_summary['losses']:>14}{new_summary['losses']:>16}")
    print(f"{'Win rate':22}{old_summary['win_rate']:>13.1f}%{new_summary['win_rate']:>15.1f}%")
    print(f"{'Total P/L':22}{old_summary['total_pl']:>14.2f}{new_summary['total_pl']:>16.2f}")
    print(f"{'Avg win':22}{old_summary['avg_win']:>14.2f}{new_summary['avg_win']:>16.2f}")
    print(f"{'Avg loss':22}{old_summary['avg_loss']:>14.2f}{new_summary['avg_loss']:>16.2f}")

    print("\nMethodology reminder: this keeps each trade's real exit price/reason unchanged and only swaps")
    print("the entry price — it does NOT re-check whether a shifted take-profit level would have been hit")
    print("sooner, later, or missed entirely, or whether EMA5/EMA9/a new cross would fire on a different")
    print("candle given a different entry moment. A first-order estimate, not a full re-simulation.")
    print("\nSeparately, and more importantly: this whole idea requires acting on a still-forming candle,")
    print("mid-candle, before a cross is actually confirmed — reopening the exact false-signal risk the")
    print("EMA5/EMA9 confirmation exists to guard against. A better average entry price here would not by")
    print("itself be a reason to deploy this; it would need to be weighed against that real added risk.")


if __name__ == "__main__":
    main()
