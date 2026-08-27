"""One-off analysis tool: simulates what would have happened to every
real ENTRY signal the ADX-momentum filter blocked (entry_blocked_adx_falling
in decisions.jsonl), had it been taken instead -- using real historical
candle data, not guesses.

Built 2026-08-27 because manually tracing each blocked signal by hand
(pulling individual candle windows one at a time) doesn't scale once
there are more than a handful in a given window -- this does the same
thing programmatically, for however many there are.

    python scripts/simulate_blocked_adx_signals.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 06:45:00" --to "2026-08-27 20:00:00"

Method, matching the exact rules the live engine uses:
  1. Find each entry_blocked_adx_falling line, parse direction +
     ema13/ema21 from its logged reason text.
  2. Fetch real candle history (bot.data.market_data.get_ohlc_range --
     already applies the broker-vs-true-UTC offset correction) covering
     the whole window plus a forward buffer, compute EMA5/13/21.
  3. Match each blocked signal to its exact confirming candle by
     comparing logged ema13/ema21 against the computed dataframe
     (floating-point tolerance) -- avoids needing to reverse-engineer
     candle boundaries by hand.
  4. Reconstruct the hypothetical entry: gap = |close - ema13|. If
     gap < gap_threshold_usd: immediate entry at that candle's close.
     Otherwise, scan forward for the first candle whose range touches
     EMA5 (matching the real EMA5-pullback rule) -- if never touched
     before the data runs out, marked NEVER FILLED.
  5. From the hypothetical entry, walk forward through real candle
     highs/lows to see whether TP or SL would have been hit first.
  6. Dollar P/L is approximated using the account's CURRENT balance-based
     lot size (bot.risk.position_sizing.calculate_lots) applied
     uniformly -- not a literal historical replay of balance at that
     exact moment, but consistent with the ~$29.64-per-$5-move pattern
     already observed across dozens of real trades tonight.

Read-only: connects to MT5 only to read historical candles, never
touches live/demo trading.
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

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots

REASON_RE = re.compile(
    r"^(BUY|SELL) cross confirmed \(ema13=([\d.]+), ema21=([\d.]+)\)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    parser.add_argument("--to", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def load_blocked_signals(account: str, since: datetime, to: datetime) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    signals = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") != "entry_blocked_adx_falling":
                continue
            ts = datetime.fromisoformat(e["timestamp"])
            if not (since <= ts <= to):
                continue
            m = REASON_RE.match(e.get("reason", ""))
            if not m:
                continue
            signals.append({
                "ts": ts,
                "direction": m.group(1),
                "ema13": float(m.group(2)),
                "ema21": float(m.group(3)),
            })
    return signals


def find_confirming_candle(df: pd.DataFrame, signal: dict) -> pd.Timestamp | None:
    """Match a blocked signal to its exact candle by comparing logged
    ema13/ema21 (rounded to 2dp in the log) against the computed
    dataframe, within a +/- 15 minute window of the log timestamp."""
    window = df[(df.index >= signal["ts"] - timedelta(minutes=15)) & (df.index <= signal["ts"] + timedelta(minutes=1))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if abs(row["ema13"] - signal["ema13"]) < 0.02 and abs(row["ema21"] - signal["ema21"]) < 0.02:
            return idx
    return None


def simulate(df: pd.DataFrame, candle_time: pd.Timestamp, direction: str, gap_threshold: float,
             stop_loss_usd: float, take_profit_usd: float) -> dict:
    row = df.loc[candle_time]
    close, ema13, ema5 = row["close"], row["ema13"], row["ema5"]
    gap = abs(close - ema13)
    forward = df[df.index > candle_time]

    if gap < gap_threshold:
        entry_price, entry_time = close, candle_time
    else:
        touch_idx = None
        for idx, r in forward.iterrows():
            if direction == "BUY" and r["low"] <= ema5:
                touch_idx = idx
                break
            if direction == "SELL" and r["high"] >= ema5:
                touch_idx = idx
                break
        if touch_idx is None:
            return {"outcome": "NEVER_FILLED", "entry_price": None}
        entry_price, entry_time = ema5, touch_idx

    if direction == "BUY":
        tp, sl = entry_price + take_profit_usd, entry_price - stop_loss_usd
    else:
        tp, sl = entry_price - take_profit_usd, entry_price + stop_loss_usd

    after_entry = df[df.index > entry_time]
    for idx, r in after_entry.iterrows():
        if direction == "BUY":
            hit_tp, hit_sl = r["high"] >= tp, r["low"] <= sl
        else:
            hit_tp, hit_sl = r["low"] <= tp, r["high"] >= sl
        if hit_tp and hit_sl:
            return {"outcome": "AMBIGUOUS_SAME_CANDLE", "entry_price": entry_price, "entry_time": entry_time}
        if hit_tp:
            return {"outcome": "WIN", "entry_price": entry_price, "entry_time": entry_time,
                     "exit_price": tp, "exit_time": idx, "distance": take_profit_usd}
        if hit_sl:
            return {"outcome": "LOSS", "entry_price": entry_price, "entry_time": entry_time,
                     "exit_price": sl, "exit_time": idx, "distance": -stop_loss_usd}
    return {"outcome": "STILL_OPEN_AT_DATA_END", "entry_price": entry_price, "entry_time": entry_time}


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.strptime(args.to, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    total_win_usd = 0.0
    total_loss_usd = 0.0
    total_wins = total_losses = total_other = 0

    for account in accounts:
        config = load_config(account)
        signals = load_blocked_signals(account, since, to)
        print(f"\n{'=' * 70}\nACCOUNT: {account}  ({len(signals)} blocked signals in window)\n{'=' * 70}")
        if not signals:
            continue

        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(hours=1), to + timedelta(hours=6))
            balance = connector.account_info().balance
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)
        lots = calculate_lots(balance, config.position_sizing)

        for sig in signals:
            candle_time = find_confirming_candle(df, sig)
            label = f"{sig['ts'].isoformat()}  {sig['direction']}"
            if candle_time is None:
                print(f"  {label}: could not match to a candle (skipped)")
                continue
            result = simulate(df, candle_time, sig["direction"], config.gap_threshold_usd,
                               config.stop_loss_usd, config.take_profit_usd)
            outcome = result["outcome"]
            if outcome == "WIN":
                usd = round(result["distance"] * lots * 100, 2)
                total_win_usd += usd
                total_wins += 1
                print(f"  {label}: WIN  entry={result['entry_price']:.2f} exit={result['exit_price']:.2f}  ~${usd:+.2f}")
            elif outcome == "LOSS":
                usd = round(result["distance"] * lots * 100, 2)
                total_loss_usd += usd
                total_losses += 1
                print(f"  {label}: LOSS entry={result['entry_price']:.2f} exit={result['exit_price']:.2f}  ~${usd:+.2f}")
            else:
                total_other += 1
                print(f"  {label}: {outcome}")

    print(f"\n{'=' * 70}\nTOTAL: {total_wins} would-be wins (~${total_win_usd:+.2f}), "
          f"{total_losses} would-be losses (~${total_loss_usd:+.2f}), {total_other} inconclusive")
    print(f"Net if all blocked signals had been taken: ~${total_win_usd + total_loss_usd:+.2f}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()