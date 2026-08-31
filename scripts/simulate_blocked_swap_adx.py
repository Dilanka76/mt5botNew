"""Simulates what would have happened to every real swap that got blocked
by the ADX gate (swap_blocked_low_adx in decisions.jsonl) had a LOWER
ADX threshold let it through -- built 2026-08-31, following up on
full_strategy_analysis.py's finding that the swap mechanism has NEVER
once confirmed a real reversal across 102 real trades on demo1_m1/
demo1_m3 (0 of 37 armed episodes), with demo1_m3 alone showing 55% of its
armings explicitly blocked by adx < 25.0.

    python scripts/simulate_blocked_swap_adx.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00" --min-adx 20

Method, matching simulate_blocked_adx_signals.py's already-validated
approach (built and cross-checked against hand-traced examples earlier
this project):
  1. Find each swap_blocked_low_adx line, parse direction + ema13/ema21 +
     the real logged ADX value (or NaN) from its reason text.
  2. Only consider blocks whose real ADX was >= --min-adx (the
     hypothetical lower threshold being tested) -- these are exactly the
     ones that WOULD have been let through under that threshold; anything
     with an even lower ADX would still be blocked regardless, so it's
     not simulated (the current threshold=25 behavior is unchanged for
     those).
  3. Fetch real candle history (bot.data.market_data.get_ohlc_range --
     already offset-corrected), compute EMA5/13/21.
  4. Match each candidate block to its exact confirming candle by
     comparing logged ema13/ema21 against the computed dataframe
     (floating-point tolerance).
  5. Simulate the hypothetical swap: a new position opens immediately at
     that candle's close, in the confirmed direction, using the account's
     real stop_loss_usd/take_profit_usd -- then walks forward through
     real candle highs/lows to see whether TP or SL would have hit first.
     (Does NOT also simulate closing the OLD held position early --
     that's a second-order effect not covered by this first pass; this
     answers "would the new reversal position itself have been a good
     trade," which is the more direct question.)
  6. Dollar P/L uses the account's CURRENT balance-based lot size
     (bot.risk.position_sizing.calculate_lots), same approximation as
     simulate_blocked_adx_signals.py.

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
    r"^(BUY|SELL) cross confirmed TWO candles in a row \(ema13=([\d.]+), ema21=([\d.]+)\) but adx=(nan|[\d.]+) < ([\d.]+)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    parser.add_argument("--to", default=None, help='"YYYY-MM-DD HH:MM:SS", true UTC (default: now)')
    parser.add_argument("--min-adx", type=float, default=20.0, help="Hypothetical lower ADX threshold to test (default: 20.0, vs the real 25.0)")
    return parser.parse_args()


def load_blocked_swaps(account: str, since: datetime, to: datetime, min_adx: float) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    signals = []
    if not path.exists():
        return signals
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") != "swap_blocked_low_adx":
                continue
            ts = datetime.fromisoformat(e["timestamp"])
            if not (since <= ts <= to):
                continue
            m = REASON_RE.match(e.get("reason", ""))
            if not m:
                continue
            adx_str = m.group(4)
            if adx_str == "nan":
                continue  # NaN ADX fails safe regardless of threshold -- not simulatable
            adx_value = float(adx_str)
            if adx_value < min_adx:
                continue  # still blocked even at the hypothetical lower threshold
            signals.append({
                "ts": ts,
                "direction": m.group(1),
                "ema13": float(m.group(2)),
                "ema21": float(m.group(3)),
                "adx": adx_value,
                "real_threshold": float(m.group(5)),
            })
    return signals


def find_confirming_candle(df: pd.DataFrame, signal: dict) -> pd.Timestamp | None:
    window = df[(df.index >= signal["ts"] - timedelta(minutes=15)) & (df.index <= signal["ts"] + timedelta(minutes=1))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if abs(row["ema13"] - signal["ema13"]) < 0.02 and abs(row["ema21"] - signal["ema21"]) < 0.02:
            return idx
    return None


def simulate(df: pd.DataFrame, candle_time: pd.Timestamp, direction: str,
             stop_loss_usd: float, take_profit_usd: float) -> dict:
    row = df.loc[candle_time]
    entry_price = row["close"]
    if direction == "BUY":
        tp, sl = entry_price + take_profit_usd, entry_price - stop_loss_usd
    else:
        tp, sl = entry_price - take_profit_usd, entry_price + stop_loss_usd

    after_entry = df[df.index > candle_time]
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
    to = (
        datetime.strptime(args.to, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if args.to else datetime.now(timezone.utc)
    )
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    total_win_usd = 0.0
    total_loss_usd = 0.0
    total_wins = total_losses = total_other = 0

    for account in accounts:
        config = load_config(account)
        signals = load_blocked_swaps(account, since, to, args.min_adx)
        print(f"\n{'=' * 78}\nACCOUNT: {account}  ({len(signals)} blocked swaps with real ADX >= {args.min_adx} "
              f"in window, i.e. would pass at a threshold of {args.min_adx})\n{'=' * 78}")
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
            label = f"{sig['ts'].isoformat()}  {sig['direction']}  real_adx={sig['adx']:.1f}"
            if candle_time is None:
                print(f"  {label}: could not match to a candle (skipped)")
                continue
            result = simulate(df, candle_time, sig["direction"], config.stop_loss_usd, config.take_profit_usd)
            outcome = result["outcome"]
            if outcome == "WIN":
                usd = round(result["distance"] * lots * 100, 2)
                total_win_usd += usd
                total_wins += 1
                print(f"  {label}: WIN  ~${usd:+.2f}")
            elif outcome == "LOSS":
                usd = round(result["distance"] * lots * 100, 2)
                total_loss_usd += usd
                total_losses += 1
                print(f"  {label}: LOSS ~${usd:+.2f}")
            else:
                total_other += 1
                print(f"  {label}: {outcome}")

    print(f"\n{'=' * 78}\nTOTAL: {total_wins} would-be wins (~${total_win_usd:+.2f}), "
          f"{total_losses} would-be losses (~${total_loss_usd:+.2f}), {total_other} inconclusive")
    print(f"Net if all these blocked swaps had been let through at ADX >= {args.min_adx}: ~${total_win_usd + total_loss_usd:+.2f}")
    print(f"{'=' * 78}")


if __name__ == "__main__":
    main()
