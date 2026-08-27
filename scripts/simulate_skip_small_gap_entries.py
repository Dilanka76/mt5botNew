"""Tests the user's newest idea -- built 2026-08-27 right after
scripts/analyze_entry_quality.py showed real small-gap "immediate" entries
barely break even (56.7% win, +$1.08 avg) vs wide-gap EMA5-pullback entries
(90% win, +$23.11 avg) -- against real historical data before touching any
live code, per the same real-data-first lesson learned from the ADX-momentum
filter (built from 2 examples, cost -$90 net once traced against the full
real sample).

The idea under test: what if EVERY entry were forced through the EMA5-
pullback rule, i.e. the "gap < gap_threshold -> enter immediately at close"
branch were removed entirely?

The user immediately flagged the real risk with this before I could even
finish proposing it: "if we get entry only the ema5 touch, anytime can this
happen, i think it risky, i mean any time next opposite candle near even
happen this" -- i.e. forcing every entry to wait for a pullback risks the
opposite cross reversing BEFORE the pullback ever happens, cancelling the
setup and missing the trade entirely. This script measures that risk
directly against real data rather than debating it in the abstract.

    python scripts/simulate_skip_small_gap_entries.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

Method:
  1. Pull every real closed trade (bot.analytics.get_closed_trades_range,
     offset-corrected) and pair each to its decisions.jsonl entry line (same
     +/-300s pairing approach as analyze_entry_quality.py) to recover its
     entry type + logged gap.
  2. Keep only real "immediate" entries (gap < gap_threshold_usd) -- these
     are the ones the new rule would change.
  3. Match each to its confirming candle (same EMA13/21-relationship
     backward-scan as analyze_entry_quality.py).
  4. From that candle, walk FORWARD candle-by-candle as if the immediate
     entry had NOT been taken: if price touches EMA5 first, that's the
     hypothetical fill -- then TP/SL is walked forward from there exactly
     like simulate_blocked_adx_signals.py. If instead the cross reverses
     (ema13/ema21 relationship flips) before any EMA5 touch, the setup is
     marked CANCELLED_BY_REVERSAL -- a real, observed instance of the exact
     risk the user raised, not a hypothetical one.
  5. Reports actual real P/L for these trades side by side with the
     hypothetical forced-pullback P/L, and how many would have been
     cancelled or left never-filled.

Dollar P/L uses the account's CURRENT balance-based lot size
(bot.risk.position_sizing.calculate_lots), same approximation approach as
simulate_blocked_adx_signals.py.

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots

GAP_RE = re.compile(r"gap=(-?\d+\.?\d*)")
ENTRY_PAIR_WINDOW_SECONDS = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def read_decisions(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    entries = []
    if not path.exists():
        return entries
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = datetime.fromisoformat(e["timestamp"])
                entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda e: e["_ts"])
    return entries


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def simulate_forced_pullback(df: pd.DataFrame, candle_time: pd.Timestamp, direction: str,
                              stop_loss_usd: float, take_profit_usd: float) -> dict:
    forward = df[df.index > candle_time]
    fill_idx = None
    for idx, r in forward.iterrows():
        if direction == "BUY" and r["ema13"] < r["ema21"]:
            return {"outcome": "CANCELLED_BY_REVERSAL"}
        if direction == "SELL" and r["ema13"] > r["ema21"]:
            return {"outcome": "CANCELLED_BY_REVERSAL"}
        ema5 = r["ema5"]
        if direction == "BUY" and r["low"] <= ema5:
            fill_idx = idx
            break
        if direction == "SELL" and r["high"] >= ema5:
            fill_idx = idx
            break
    if fill_idx is None:
        return {"outcome": "NEVER_FILLED"}

    entry_price = df.loc[fill_idx, "ema5"]
    if direction == "BUY":
        tp, sl = entry_price + take_profit_usd, entry_price - stop_loss_usd
    else:
        tp, sl = entry_price - take_profit_usd, entry_price + stop_loss_usd

    after_entry = df[df.index > fill_idx]
    for idx, r in after_entry.iterrows():
        if direction == "BUY":
            hit_tp, hit_sl = r["high"] >= tp, r["low"] <= sl
        else:
            hit_tp, hit_sl = r["low"] <= tp, r["high"] >= sl
        if hit_tp and hit_sl:
            return {"outcome": "AMBIGUOUS_SAME_CANDLE"}
        if hit_tp:
            return {"outcome": "WIN", "distance": take_profit_usd}
        if hit_sl:
            return {"outcome": "LOSS", "distance": -stop_loss_usd}
    return {"outcome": "STILL_OPEN_AT_DATA_END"}


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    grand_actual = grand_hypo = 0.0
    grand_n = grand_cancelled = grand_never = 0

    for account in accounts:
        config = load_config(account)
        decisions = read_decisions(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            mt5_trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(hours=2), to + timedelta(hours=6))
            balance = connector.account_info().balance
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        lots = calculate_lots(balance, config.position_sizing)

        entries = [e for e in decisions if e.get("action") == "trade_entered" and e["_ts"] >= since]
        used_idx: set[int] = set()

        print(f"\n{'=' * 78}\nACCOUNT: {account}  (gap_threshold=${config.gap_threshold_usd})\n{'=' * 78}")

        account_actual = account_hypo = 0.0
        n = cancelled = never = 0

        for t in mt5_trades:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            best_idx, best_delta = None, None
            for i, e in enumerate(entries):
                if i in used_idx or e.get("direction") != t["direction"]:
                    continue
                delta = abs((e["_ts"] - entry_utc).total_seconds())
                if delta <= ENTRY_PAIR_WINDOW_SECONDS and (best_delta is None or delta < best_delta):
                    best_idx, best_delta = i, delta
            if best_idx is None:
                continue
            used_idx.add(best_idx)
            reason = entries[best_idx].get("reason", "")
            entry_type = "ema5_touch" if reason.startswith("EMA5 touch") else "immediate"
            if entry_type != "immediate":
                continue
            m = GAP_RE.search(reason)
            if m is None:
                continue
            gap = float(m.group(1))
            if gap >= config.gap_threshold_usd:
                continue

            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            if candle_time is None:
                continue

            n += 1
            account_actual += t["profit"]

            result = simulate_forced_pullback(df, candle_time, t["direction"], config.stop_loss_usd, config.take_profit_usd)
            outcome = result["outcome"]
            if outcome in ("WIN", "LOSS"):
                usd = round(result["distance"] * lots * 100, 2)
                account_hypo += usd
                print(f"  {entry_utc.isoformat()} {t['direction']}  actual=${t['profit']:+.2f}  hypo(forced-pullback)={outcome} ${usd:+.2f}")
            elif outcome == "CANCELLED_BY_REVERSAL":
                cancelled += 1
                print(f"  {entry_utc.isoformat()} {t['direction']}  actual=${t['profit']:+.2f}  hypo(forced-pullback)=CANCELLED (opposite cross before EMA5 touch -- MISSED entirely)")
            elif outcome == "NEVER_FILLED":
                never += 1
                print(f"  {entry_utc.isoformat()} {t['direction']}  actual=${t['profit']:+.2f}  hypo(forced-pullback)=NEVER FILLED before data ran out")
            else:
                print(f"  {entry_utc.isoformat()} {t['direction']}  actual=${t['profit']:+.2f}  hypo(forced-pullback)={outcome}")

        print(f"\n  Account summary: {n} real immediate small-gap entries analyzed")
        print(f"    Actual P/L (real, as traded):                 ${account_actual:+.2f}")
        print(f"    Hypothetical P/L (forced EMA5-pullback rule): ${account_hypo:+.2f}  ({cancelled} CANCELLED by reversal, {never} never filled)")
        print(f"    Difference:                                   ${account_hypo - account_actual:+.2f}")

        grand_actual += account_actual
        grand_hypo += account_hypo
        grand_n += n
        grand_cancelled += cancelled
        grand_never += never

    print(f"\n{'=' * 78}\nTOTAL across accounts: {grand_n} real immediate small-gap entries")
    print(f"  Actual P/L (real, as traded):                 ${grand_actual:+.2f}")
    print(f"  Hypothetical P/L (forced EMA5-pullback rule): ${grand_hypo:+.2f}")
    print(f"  {grand_cancelled} would have been CANCELLED entirely (opposite cross hit before EMA5 touch) -- real evidence of the cancellation risk flagged")
    print(f"  {grand_never} never got filled before data ran out (still pending)")
    print(f"  Net difference if ALL entries were forced through EMA5-pullback: ${grand_hypo - grand_actual:+.2f}")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    main()
