"""Verifies one real EMA13/21 cross against real MT5 candle data, and
shows the gap calculated TWO ways side by side:

  - CODEBASE (calculate_gap, current code): cross candle's own OPEN
    price vs EMA13 — the finalized design (see
    docs/STRATEGY_PROPOSED_OPEN_GAP.md). NOTE: this is what's in the
    repo NOW, which is NOT necessarily what any given already-running
    main.py process is actually using — a live Python process keeps
    running whatever code was loaded at its own startup, unaffected by
    a later `git pull`, until it's explicitly restarted. Check the
    process's actual start time/pid before assuming this reflects live
    behavior.
  - SUPERSEDED (close-based): the OLD formula, cross candle's own
    CLOSE price vs EMA13 — computed independently here since
    calculate_gap() no longer does this; kept only for comparison.

This is a one-instance manual verification, not a backtest — the user
wants to check one specific real cross (e.g. near 19:30 Colombo time on
a given date) against what they see on their own chart before any
broader comparison is built. Nothing here changes any strategy code;
calculate_gap() and detect_all_crosses() are read-only, unmodified
imports from the real engine.

    python scripts/verify_cross_gap_openprice.py --account demo1 --datetime 2026-08-13T19:30

Read-only: only fetches historical OHLC, same as scripts/verify_candle_utc.py
and scripts/check_history_range.py — never touches live/demo trading or
places any order.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.strategy.cross_detector import Direction, calculate_gap, detect_all_crosses

COLOMBO = ZoneInfo("Asia/Colombo")
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument(
        "--datetime", dest="target_dt", required=True,
        help="Colombo local time, e.g. 2026-08-13T19:30 (NOT UTC — converted internally).",
    )
    parser.add_argument(
        "--window-minutes", type=int, default=120,
        help="How far around --datetime to search for the nearest real cross (default 120).",
    )
    return parser.parse_args()


def superseded_close_based_gap(direction: Direction, cross_candle_close: float, ema13: float) -> float:
    """The OLD formula, cross candle's own CLOSE price vs EMA13 — no
    longer what bot.strategy.cross_detector.calculate_gap() computes
    (that function was changed to the open-based formula as part of
    implementing docs/STRATEGY_PROPOSED_OPEN_GAP.md). Computed
    independently here purely for side-by-side comparison — not used
    anywhere in the live engine or backtest."""
    if direction == Direction.BUY:
        return cross_candle_close - ema13
    return ema13 - cross_candle_close


def main() -> None:
    args = parse_args()
    config = load_config(args.account)

    target_local = datetime.strptime(args.target_dt, "%Y-%m-%dT%H:%M").replace(tzinfo=COLOMBO)
    target_utc = target_local.astimezone(ZoneInfo("UTC"))
    print(f"Target: {target_local.isoformat()} (Colombo) = {target_utc.isoformat()} (UTC)")
    print(f"Searching for the nearest real EMA13/21 cross within +/-{args.window_minutes} minutes.\n")

    window_start = target_utc - timedelta(minutes=args.window_minutes)
    window_end = target_utc + timedelta(minutes=args.window_minutes)
    minutes_per_candle = TIMEFRAME_MINUTES[config.timeframe]
    warmup_start = window_start - timedelta(minutes=config.candles_to_fetch * minutes_per_candle)

    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        info = connector.account_info()
        print(f"Connected to MT5 as: login={info.login} server={info.server!r} balance={info.balance:.2f}\n")
        df = get_ohlc_range(connector, config.symbol, config.timeframe, warmup_start, window_end)
    finally:
        connector.disconnect()  # read-only fetch — never touches live/demo trading

    df = compute_emas(df, config.ema_periods)

    # Run detect_all_crosses on the FULL fetched df (not just the window)
    # so the EMA13/21 state has proper context leading into the window —
    # otherwise a cross right at the window's start could be missed or
    # misclassified. Filter down to the window only after detecting.
    all_events = detect_all_crosses(df)
    events = [e for e in all_events if window_start <= e.candle_time <= window_end]

    if not events:
        raise SystemExit(
            f"No cross found within +/-{args.window_minutes} minutes of {target_local.isoformat()}. "
            f"Try a wider --window-minutes, or double-check the date/time."
        )

    nearest = min(events, key=lambda e: abs((e.candle_time - target_utc).total_seconds()))
    others = [e for e in events if e is not nearest]

    cross_pos = df.index.get_loc(nearest.candle_time)
    if isinstance(cross_pos, slice):  # duplicate timestamps, shouldn't happen post-dedup, but be safe
        cross_pos = cross_pos.start
    if cross_pos + 1 >= len(df):
        raise SystemExit(f"Cross at {nearest.candle_time} is the last candle fetched — widen the range.")
    next_candle = df.iloc[cross_pos + 1]

    cross_local = nearest.candle_time.astimezone(COLOMBO)
    print(f"Nearest cross found: {nearest.direction.value} at {nearest.candle_time.isoformat()} (UTC) "
          f"= {cross_local.isoformat()} (Colombo)")
    print(f"  (this was {abs((nearest.candle_time - target_utc).total_seconds()) / 60:.1f} minutes from your target time)\n")

    if others:
        print(f"Note: {len(others)} other cross(es) also fell within the search window — showing the closest one only:")
        for e in others:
            print(f"  {e.direction.value} at {e.candle_time.astimezone(COLOMBO).isoformat()} (Colombo)")
        print()

    if cross_pos - 1 < 0:
        raise SystemExit(f"Cross at {nearest.candle_time} is the first candle fetched — widen the range to see the candle before it.")
    prev_candle = df.iloc[cross_pos - 1]
    prev_ema13 = float(df["ema13"].iloc[cross_pos - 1])
    prev_ema21 = float(df["ema21"].iloc[cross_pos - 1])
    prev_state = "ABOVE (bullish)" if prev_ema13 > prev_ema21 else ("BELOW (bearish)" if prev_ema13 < prev_ema21 else "EQUAL")
    curr_state = "ABOVE (bullish)" if nearest.ema13 > nearest.ema21 else ("BELOW (bearish)" if nearest.ema13 < nearest.ema21 else "EQUAL")

    print("Previous candle (right before the cross candle — this is where EMA13/21 still showed the OLD relationship):")
    print(f"  time (UTC):     {prev_candle.name.isoformat()}")
    print(f"  time (Colombo): {prev_candle.name.astimezone(COLOMBO).isoformat()}")
    print(f"  close:  {prev_candle['close']:.2f}")
    print(f"  ema13:  {prev_ema13:.2f}")
    print(f"  ema21:  {prev_ema21:.2f}")
    print(f"  state:  {prev_state}")
    print("  (the 'equal point' sits visually between THIS candle and the next one below)\n")

    cross_candle = df.iloc[cross_pos]
    print("Cross candle (the very next candle after the equal point — this is where the NEW relationship first appears, and where entry happens):")
    print(f"  time (UTC):     {nearest.candle_time.isoformat()}")
    print(f"  time (Colombo): {cross_local.isoformat()}")
    print(f"  open:   {cross_candle['open']:.2f}")
    print(f"  high:   {cross_candle['high']:.2f}")
    print(f"  low:    {cross_candle['low']:.2f}")
    print(f"  close:  {nearest.close:.2f}")
    print(f"  ema13:  {nearest.ema13:.2f}")
    print(f"  ema21:  {nearest.ema21:.2f}")
    print(f"  state:  {curr_state}  <-- flipped from '{prev_state}' the candle before, confirming the cross HERE")

    print("\nNext candle (starts forming the instant the cross is confirmed):")
    print(f"  time (UTC):     {next_candle.name.isoformat()}")
    print(f"  time (Colombo): {next_candle.name.astimezone(COLOMBO).isoformat()}")
    print(f"  open:   {next_candle['open']:.2f}")
    print(f"  high:   {next_candle['high']:.2f}")
    print(f"  low:    {next_candle['low']:.2f}")

    codebase_gap = calculate_gap(nearest)
    superseded_gap = superseded_close_based_gap(nearest.direction, nearest.close, nearest.ema13)
    threshold = config.gap_threshold_usd

    print(f"\nGap threshold for this account: ${threshold:.2f}\n")
    print(f"{'Method':<32}{'Gap':>10}{'Decision':>28}")
    print("-" * 70)
    codebase_decision = "IMMEDIATE entry" if codebase_gap < threshold else "WAIT for EMA5 touch"
    superseded_decision = "IMMEDIATE entry" if superseded_gap < threshold else "WAIT for EMA5 touch"
    print(f"{'CODEBASE (open-based, now)':<32}{codebase_gap:>10.2f}{codebase_decision:>28}")
    print(f"{'SUPERSEDED (close-based, old)':<32}{superseded_gap:>10.2f}{superseded_decision:>28}")

    print()
    if codebase_decision == superseded_decision:
        print(f"Both formulas agree on this trade: {codebase_decision}. The price difference between the")
        print("cross candle's own open and close was too small to change the outcome here.")
    else:
        print("*** These formulas DISAGREE on this specific trade. ***")
        print(f"Codebase (open-based) says: {codebase_decision}")
        print(f"Superseded (close-based) says: {superseded_decision}")
        print("This is a real, concrete case where the two formulas would have made a different decision.")
    print()
    print("Reminder: 'CODEBASE' reflects what's in the repo right now, not necessarily what any given")
    print("already-running main.py process is actually executing — check its actual pid/start time first.")


if __name__ == "__main__":
    main()
