"""Two questions in one pass, both raised 2026-09-04:

PART 1 -- FILTER OVERLAP (M3 legs): the colour and volume filters both
show strong forward results on demo1_m3/demo2_m3
(scripts/check_shadow_filter_forward_results.py). But do they flag the
SAME trades? If they overlap heavily they are one filter wearing two
hats, and their benefits cannot be added. Cross-tabulates the two shadow
flags against real outcomes.

PART 2 -- MARKET REGIME: the user's own framing, and the missing piece
in every analysis so far: "some days gold can drop, or go up, or be
trending... that causes the effect some days in our strategy." This
project has repeatedly concluded "regime-dependent" without ever
MEASURING regime. This does.

Regime measure: Kaufman's Efficiency Ratio over the LOOKBACK candles
ending at (and including) each trade's confirming candle --

    ER = |close[t] - close[t-n]| / sum(|close[i] - close[i-1]|)

Net directional movement divided by total distance travelled. Near 1.0 =
clean trend (price went somewhere). Near 0 = chop (lots of movement,
no progress). Causal by construction -- only uses candles at or before
entry, no lookahead.

An EMA13/21 crossover system is a trend-following design, so the
hypothesis is that outcomes should improve with ER. If real trades
confirm that, regime is a far more powerful lever than any
single-candle filter, because it explains WHOLE DAYS rather than
individual entries.

    python scripts/analyze_regime_and_filter_overlap.py --since "2026-08-25 00:00:00"

Read-only: connects to MT5 only to read historical data, never touches
live/demo trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

ALL_ACCOUNTS = ["demo1_m1", "demo1_m3", "demo2_m1", "demo2_m3"]
M3_ACCOUNTS = ["demo1_m3", "demo2_m3"]
MATCH_WINDOW = timedelta(minutes=5)
LOOKBACK = 20  # candles for the efficiency ratio


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def read_entries(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "trade_entered":
            continue
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        out.append(e)
    return out


def match(entries: list[dict], trades: list[dict]) -> list[tuple[dict, dict]]:
    used, matched = set(), []
    for e in entries:
        best_i, best_score = None, None
        for i, t in enumerate(trades):
            if i in used or t["direction"] != e.get("direction"):
                continue
            if abs((t["entry_time"].astimezone(timezone.utc) - e["_ts"]).total_seconds()) > MATCH_WINDOW.total_seconds():
                continue
            d = abs(t["entry_price"] - e.get("entry", 0.0))
            if d > 0.10:
                continue
            if best_score is None or d < best_score:
                best_i, best_score = i, d
        if best_i is not None:
            used.add(best_i)
            matched.append((e, trades[best_i]))
    return matched


def efficiency_ratio(df: pd.DataFrame, candle_time: pd.Timestamp, lookback: int = LOOKBACK) -> float | None:
    pos = df.index.get_loc(candle_time)
    if pos < lookback:
        return None
    window = df["close"].iloc[pos - lookback:pos + 1]
    net = abs(float(window.iloc[-1]) - float(window.iloc[0]))
    total = float(window.diff().abs().sum())
    if total <= 0:
        return None
    return net / total


def find_confirming_candle(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    w = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(w.index):
        row = w.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def summarize(label: str, profits: list[float]) -> str:
    if not profits:
        return f"{label:<34} n=0"
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    total = sum(profits)
    return (f"{label:<34} n={n:<4} {100*wins/n:5.1f}% win  total ${total:+9.2f}  "
            f"avg ${total/n:+7.2f}/trade")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    # ---------------- PART 1: filter overlap on the M3 legs ----------------
    print(f"{'#' * 78}\n# PART 1 -- do colour and volume flag the SAME trades on M3?\n{'#' * 78}\n")
    for account in M3_ACCOUNTS:
        entries = [e for e in read_entries(account)
                   if "shadow_closed_in_favor" in e and "shadow_low_volume" in e]
        if not entries:
            print(f"{account}: no entries carrying both flags yet.\n")
            continue
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            trades = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                             min(e["_ts"] for e in entries) - timedelta(minutes=1), now, offset)
        finally:
            connector.disconnect()
        pairs = match(entries, trades)

        groups: dict[str, list[float]] = {"both skip": [], "colour only": [], "volume only": [], "neither (kept)": []}
        for e, t in pairs:
            colour_skips = not e["shadow_closed_in_favor"]
            volume_skips = bool(e["shadow_low_volume"])
            if colour_skips and volume_skips:
                key = "both skip"
            elif colour_skips:
                key = "colour only"
            elif volume_skips:
                key = "volume only"
            else:
                key = "neither (kept)"
            groups[key].append(t["profit"])

        print(f"{'=' * 78}\n{account}: {len(pairs)} trades with both flags\n{'=' * 78}")
        for key in ("both skip", "colour only", "volume only", "neither (kept)"):
            print("  " + summarize(key, groups[key]))
        skipped_any = groups["both skip"] + groups["colour only"] + groups["volume only"]
        print("  " + summarize("ANY filter would skip", skipped_any))
        overlap = len(groups["both skip"])
        total_skipped = len(skipped_any)
        if total_skipped:
            print(f"  -> overlap: {overlap}/{total_skipped} skipped trades ({100*overlap/total_skipped:.0f}%) "
                  f"are flagged by BOTH filters")
        print()

    # ---------------- PART 2: market regime ----------------
    print(f"\n{'#' * 78}\n# PART 2 -- does market regime (Efficiency Ratio) predict outcome?\n"
          f"# ER near 1.0 = clean trend, near 0 = chop. Lookback {LOOKBACK} candles.\n{'#' * 78}\n")
    for account in ALL_ACCOUNTS:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number, since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since - timedelta(days=5), now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        rows = []
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            ct = find_confirming_candle(df, entry_utc, t["direction"])
            if ct is None:
                continue
            er = efficiency_ratio(df, ct)
            if er is None:
                continue
            rows.append({"er": er, "profit": t["profit"], "time": entry_utc})
        if not rows:
            print(f"{account}: no matched trades.\n")
            continue

        ers = sorted(r["er"] for r in rows)
        t33, t66 = ers[len(ers) // 3], ers[2 * len(ers) // 3]
        buckets = {"1_chop (low ER)": [], "2_medium": [], "3_trending (high ER)": []}
        for r in rows:
            key = ("1_chop (low ER)" if r["er"] < t33
                   else "2_medium" if r["er"] < t66 else "3_trending (high ER)")
            buckets[key].append(r["profit"])

        print(f"{'=' * 78}\n{account}: {len(rows)} trades  (ER tertiles at {t33:.3f} / {t66:.3f})\n{'=' * 78}")
        for key in ("1_chop (low ER)", "2_medium", "3_trending (high ER)"):
            print("  " + summarize(key, buckets[key]))

        # Walk-forward: does the chop-vs-trend gap hold in both halves?
        rows.sort(key=lambda r: r["time"])
        mid = len(rows) // 2
        for half_label, half in (("first half ", rows[:mid]), ("second half", rows[mid:])):
            chop = [r["profit"] for r in half if r["er"] < t33]
            trend = [r["profit"] for r in half if r["er"] >= t66]
            c_avg = sum(chop) / len(chop) if chop else 0.0
            t_avg = sum(trend) / len(trend) if trend else 0.0
            print(f"    {half_label}: chop avg ${c_avg:+7.2f}/trade (n={len(chop)})  vs  "
                  f"trending avg ${t_avg:+7.2f}/trade (n={len(trend)})")
        print()


if __name__ == "__main__":
    main()
