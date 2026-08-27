"""Mines every real closed trade so far to find which entry
characteristics actually correlate with wins vs losses in real data --
built 2026-08-27 at the user's explicit request for a "professional,
master mind" way to verify entry quality. Evidence-first, not
indicator-first: the ADX-momentum entry filter built earlier tonight
was based on just 2 real examples and turned out net negative (-$90)
once traced against the full real sample. This tool checks what
actually predicts a win in OUR OWN real trade history before any new
rule gets proposed.

    python scripts/analyze_entry_quality.py --accounts demo1_m1,demo1_m3 --since "2026-08-25 00:00:00"

For every real closed trade (via bot.analytics.get_closed_trades_range,
already offset-corrected), pulls entry type + gap size from
decisions.jsonl (same pairing approach as
scripts/generate_live_test_report.py), then looks up the CONFIRMING
CANDLE's own real OHLC + tick volume from real MT5 candle history
(matched the same way scripts/simulate_blocked_adx_signals.py matches a
blocked signal to its candle) to compute:
  - candle body size (|close-open|) and how decisive it was (body as a
    fraction of the candle's own high-low range, and whether it closed
    in the trade's favor within that range)
  - real tick volume on that confirming candle
  - EMA13/EMA21 separation at the moment of entry (a direct trend-
    strength read, distinct from ADX)

Breaks win rate and avg P/L down by entry type, gap-size bucket, candle
body-size bucket, volume bucket, EMA-separation bucket, and hour of day
(app time) -- so what to build next (if anything) is grounded in real
outcomes, not a plausible-sounding theory.

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
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

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
    """The confirming candle is the last CLOSED candle before/at the
    entry-related timestamp whose EMA13/21 relationship matches the
    trade's direction (BUY -> ema13>ema21, SELL -> ema13<ema21) --
    scans backward from `near` within a reasonable window."""
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def bucket(value: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels):
        if value <= edge:
            return label
    return labels[-1]


def summarize(rows: list[dict], key_fn, label: str) -> None:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    print(f"\n  By {label}:")
    for key in sorted(groups.keys()):
        g = groups[key]
        wins = [r for r in g if r["profit"] > 0]
        win_rate = 100 * len(wins) / len(g) if g else 0.0
        avg_pl = mean(r["profit"] for r in g)
        print(f"    {key:<20} n={len(g):>3}  win_rate={win_rate:5.1f}%  avg_P/L=${avg_pl:+7.2f}  total=${sum(r['profit'] for r in g):+8.2f}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    all_rows: list[dict] = []

    for account in accounts:
        config = load_config(account)
        decisions = read_decisions(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            mt5_trades = get_closed_trades_range(config.symbol, config.execution.magic_number, since, to, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(hours=2), to)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        entries = [e for e in decisions if e.get("action") == "trade_entered" and e["_ts"] >= since]
        used_idx: set[int] = set()

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
            entry_type, gap = "unknown", None
            if best_idx is not None:
                used_idx.add(best_idx)
                reason = entries[best_idx].get("reason", "")
                entry_type = "ema5_touch" if reason.startswith("EMA5 touch") else "immediate"
                m = GAP_RE.search(reason)
                if m:
                    gap = float(m.group(1))

            candle_time = find_confirming_candle(df, entry_utc, t["direction"])
            if candle_time is None:
                continue
            row = df.loc[candle_time]
            body = abs(row["close"] - row["open"])
            candle_range = row["high"] - row["low"]
            body_ratio = body / candle_range if candle_range > 0 else 0.0
            if t["direction"] == "BUY":
                closed_in_favor = row["close"] > row["open"]
            else:
                closed_in_favor = row["close"] < row["open"]
            ema_gap = abs(row["ema13"] - row["ema21"])
            volume = float(row.get("tick_volume", 0) or 0)
            hour = entry_utc.hour

            all_rows.append({
                "account": account, "direction": t["direction"], "profit": t["profit"],
                "outcome": "WIN" if t["profit"] > 0 else ("LOSS" if t["profit"] < 0 else "BREAKEVEN"),
                "entry_type": entry_type, "gap": gap, "body": body, "body_ratio": body_ratio,
                "closed_in_favor": closed_in_favor, "ema_gap": ema_gap, "volume": volume, "hour": hour,
            })

    if not all_rows:
        print("No matched trades in this window.")
        return

    print(f"\n{'=' * 70}\nENTRY QUALITY ANALYSIS — {len(all_rows)} real trades matched to their confirming candle\n{'=' * 70}")
    wins = [r for r in all_rows if r["profit"] > 0]
    print(f"Overall: {len(wins)}/{len(all_rows)} wins ({100 * len(wins) / len(all_rows):.1f}%), "
          f"total P/L ${sum(r['profit'] for r in all_rows):+.2f}")

    summarize(all_rows, lambda r: r["entry_type"], "entry type")
    summarize(all_rows, lambda r: "YES" if r["closed_in_favor"] else "NO", "confirming candle closed IN the trade's favor")

    gaps = [r["gap"] for r in all_rows if r["gap"] is not None]
    if gaps:
        edges = sorted(gaps)
        e33, e66 = edges[len(edges) // 3], edges[2 * len(edges) // 3]
        summarize([r for r in all_rows if r["gap"] is not None],
                   lambda r: bucket(r["gap"], [e33, e66], ["1_small", "2_medium", "3_large"]), "gap size (tertiles)")

    bodies = sorted(r["body"] for r in all_rows)
    b33, b66 = bodies[len(bodies) // 3], bodies[2 * len(bodies) // 3]
    summarize(all_rows, lambda r: bucket(r["body"], [b33, b66], ["1_small", "2_medium", "3_large"]), "confirming candle body size (tertiles)")

    ratios = sorted(r["body_ratio"] for r in all_rows)
    r33, r66 = ratios[len(ratios) // 3], ratios[2 * len(ratios) // 3]
    summarize(all_rows, lambda r: bucket(r["body_ratio"], [r33, r66], ["1_indecisive", "2_medium", "3_decisive"]), "candle decisiveness (body/range, tertiles)")

    vols = sorted(r["volume"] for r in all_rows)
    v33, v66 = vols[len(vols) // 3], vols[2 * len(vols) // 3]
    summarize(all_rows, lambda r: bucket(r["volume"], [v33, v66], ["1_low", "2_medium", "3_high"]), "tick volume on confirming candle (tertiles)")

    egaps = sorted(r["ema_gap"] for r in all_rows)
    eg33, eg66 = egaps[len(egaps) // 3], egaps[2 * len(egaps) // 3]
    summarize(all_rows, lambda r: bucket(r["ema_gap"], [eg33, eg66], ["1_tight", "2_medium", "3_wide"]), "EMA13/21 separation at entry (tertiles)")

    summarize(all_rows, lambda r: f"{r['hour']:02d}:00 UTC", "hour of day (true UTC)")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()