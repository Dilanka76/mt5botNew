"""Is the SELL bias a real edge, or just the market falling?

The finding (2026-09-08, scripts/analyze_loss_anatomy.py): on BOTH M3
accounts, in BOTH walk-forward halves, BUY loses and SELL wins --
demo1_m3 BUY -$18.92/trade vs SELL +$30.93, demo2_m3 BUY -$5.54 vs SELL
+$18.96. Four out of four. Nothing else in this project has passed that
cleanly.

But gold FELL through the whole sample (roughly $4,473 on 2026-09-04 to
$4,398 on 09-07). In a falling market SELLs win, and that is not an edge
-- it is the direction of the market. Trading SELL-only on the strength
of it would be a bet that gold keeps falling, and it reverses the day it
does not.

THE TEST: split a long backtest into weeks where gold ROSE and weeks
where it FELL, then compare BUY against SELL inside each group.

  - SELL beats BUY in BOTH -> a real, direction-independent edge, and
    the biggest finding in this project.
  - SELL beats BUY only in falling weeks -> it is the trend. Ignore it.

The whole question is what happens in the RISING weeks, which the two
weeks of real trades contain almost none of. That is why this needs a
backtest over months rather than the live history.

STEP 1 -- generate the trades (months, not weeks; needs both regimes):
    python scripts/backtest.py --account demo2_m3 --from 2026-03-01 --to 2026-09-01

STEP 2 -- this script, pointed at the trades file it wrote:
    python scripts/analyze_direction_vs_trend.py --trades reports/backtest/<name>.trades.jsonl --account demo2_m3

Weekly direction is measured from real candles: the close of the week's
last candle minus the close of its first. A week whose move is smaller
than --flat-threshold is reported separately rather than forced into
"rose" or "fell", since a flat week tests nothing.

Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trades", required=True, help="a backtest .trades.jsonl file")
    p.add_argument("--account", required=True, type=validate_account_name)
    p.add_argument("--flat-threshold", type=float, default=10.0,
                   help="a week moving less than this many dollars counts as FLAT (default 10)")
    return p.parse_args()


def load_trades(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
            rows.append({
                "direction": t["direction"],
                "profit": float(t["profit"]),
                "open_time": datetime.fromisoformat(t["open_time"]),
            })
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return rows


def report(label: str, rows: list[dict]) -> None:
    if not rows:
        print(f"  {label:<22} no trades")
        return
    parts = []
    for d in ("BUY", "SELL"):
        sub = [r for r in rows if r["direction"] == d]
        if not sub:
            parts.append(f"{d}: none")
            continue
        wins = sum(1 for r in sub if r["profit"] > 0)
        avg = sum(r["profit"] for r in sub) / len(sub)
        parts.append(f"{d} n={len(sub):<4} {100 * wins / len(sub):>5.1f}% win  ${avg:+7.2f}/trade")
    print(f"  {label:<22} " + "   |   ".join(parts))


def main() -> None:
    args = parse_args()
    path = Path(args.trades)
    if not path.is_file():
        raise SystemExit(f"{path} not found -- run scripts/backtest.py first (see this script's header).")

    trades = load_trades(path)
    if not trades:
        raise SystemExit(f"No usable trades in {path}.")
    trades.sort(key=lambda t: t["open_time"])
    first, last = trades[0]["open_time"], trades[-1]["open_time"]
    print(f"{len(trades)} backtested trades, {first:%Y-%m-%d} to {last:%Y-%m-%d}\n")

    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        df = get_ohlc_range(connector, config.symbol, config.timeframe,
                            first - timedelta(days=10), last + timedelta(days=10))
    finally:
        connector.disconnect()

    # Net move per ISO week, from real candles.
    closes = df["close"]
    weekly: dict[tuple[int, int], float] = {}
    for ts, close in closes.items():
        key = (ts.isocalendar().year, ts.isocalendar().week)
        weekly.setdefault(key, [close, close])
        weekly[key][1] = close
    week_move = {k: v[1] - v[0] for k, v in weekly.items()}

    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        ts = pd.Timestamp(t["open_time"])
        key = (ts.isocalendar().year, ts.isocalendar().week)
        move = week_move.get(key)
        if move is None:
            continue
        if abs(move) < args.flat_threshold:
            buckets["FLAT"].append(t)
        elif move > 0:
            buckets["ROSE"].append(t)
        else:
            buckets["FELL"].append(t)

    rose = sum(1 for m in week_move.values() if m >= args.flat_threshold)
    fell = sum(1 for m in week_move.values() if m <= -args.flat_threshold)
    flat = len(week_move) - rose - fell
    print(f"weeks in this window: {rose} rose, {fell} fell, {flat} flat "
          f"(flat = moved less than ${args.flat_threshold:.0f})\n")

    print("BUY vs SELL, split by what the market did that week")
    print("-" * 84)
    report("weeks gold FELL", buckets["FELL"])
    report("weeks gold ROSE", buckets["ROSE"])
    report("flat weeks", buckets["FLAT"])
    print("-" * 84)
    report("everything pooled", trades)

    print()
    rose_rows = buckets["ROSE"]
    if not rose_rows:
        print("VERDICT: no rising weeks in this window -- the test could not run.")
        print("Extend the date range until it covers months when gold went UP.")
        return
    buy = [r["profit"] for r in rose_rows if r["direction"] == "BUY"]
    sell = [r["profit"] for r in rose_rows if r["direction"] == "SELL"]
    if not buy or not sell:
        print("VERDICT: rising weeks contain only one direction -- inconclusive.")
        return
    buy_avg, sell_avg = sum(buy) / len(buy), sum(sell) / len(sell)
    print(f"VERDICT, from the RISING weeks only (n={len(buy)} BUY, {len(sell)} SELL):")
    if sell_avg > buy_avg:
        print(f"  SELL still beats BUY when gold ROSE (${sell_avg:+.2f} vs ${buy_avg:+.2f}/trade).")
        print("  That is direction-independent, so it is a REAL edge and not the trend.")
        print("  Confirm it holds in each rising month separately before acting on it.")
    else:
        print(f"  BUY beats SELL when gold ROSE (${buy_avg:+.2f} vs ${sell_avg:+.2f}/trade).")
        print("  So the SELL bias in the live data is the MARKET FALLING, not an edge.")
        print("  A SELL-only rule would be a bet on gold continuing to fall, and would")
        print("  have lost money in these months. Do not deploy it.")


if __name__ == "__main__":
    main()
