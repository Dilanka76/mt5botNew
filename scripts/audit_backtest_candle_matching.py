"""Did the backtest scripts read the WRONG candle?

The engine records, on every real entry, the exact colour/volume values
it decided on (`shadow_closed_in_favor`, `shadow_low_volume`,
`shadow_tick_volume`) -- computed from `df.iloc[-2]`, the candle that
actually closed and triggered the cross. That is ground truth.

The backtest scripts (`simulate_demo3_entry_filter.py`,
`analyze_entry_quality.py`, `analyze_trend_filter.py` and others) locate
the candle themselves with:

    window = df[(df.index <= near) & (df.index >= near - 30min)]
    for idx in reversed(window.index):
        if <EMA13 is on the right side of EMA21>: return idx

Two suspected problems, both found while fixing a related bug on
2026-09-04 (see project_trade_protection_findings):
  1. it matches a candle by STATE ("13 is above 21"), not by STATE
     CHANGE ("13 just crossed above 21"), and
  2. `df.index <= near` admits the candle that STARTED at the entry
     moment -- for a trade entered 09:09:01 the candle indexed 09:09
     qualifies and, being latest, wins -- while the engine used 09:06.

If either bites, the backtest measured a candle the engine never looked
at, and every conclusion drawn from those scripts is about the wrong
data. That matters because `simulate_demo3_entry_filter.py` produced the
entire case for the colour+volume filter now live on demo1_m3 -- a
filter whose live scorecard (5 of 7 blocks wrong) contradicts its
backtest (92% of blocks predicted to be losers).

This settles it by replaying the backtest's own candle-picker against
every real entry that carries engine-logged shadow values, and comparing
the two field by field.

    python scripts/audit_backtest_candle_matching.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3

Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector

CANDLES_TO_FETCH = 500


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    return p.parse_args()


def backtest_candle_picker(df: pd.DataFrame, near: datetime, direction: str) -> pd.Timestamp | None:
    """VERBATIM copy of the picker used by simulate_demo3_entry_filter.py
    and friends -- reproduced here deliberately, not imported, so this
    audit tests what those scripts actually do."""
    window = df[(df.index <= near) & (df.index >= near - timedelta(minutes=30))]
    for idx in reversed(window.index):
        row = window.loc[idx]
        if direction == "BUY" and row["ema13"] > row["ema21"]:
            return idx
        if direction == "SELL" and row["ema13"] < row["ema21"]:
            return idx
    return None


def read_shadow_entries(account: str) -> list[dict]:
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
        if e.get("action") != "trade_entered" or "shadow_closed_in_favor" not in e:
            continue
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        out.append(e)
    return out


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]
    grand_total = grand_mismatch = 0

    for account in accounts:
        entries = read_shadow_entries(account)
        if not entries:
            print(f"{account}: no shadow-logged entries.\n")
            continue

        config = load_config(account)
        since = min(e["_ts"] for e in entries) - timedelta(days=2)
        now = datetime.now(timezone.utc)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            mt5_utc_offset(connector, config.symbol)
            df = get_ohlc_range(connector, config.symbol, config.timeframe, since, now)
        finally:
            connector.disconnect()
        df = compute_emas(df, config.ema_periods)

        checked = colour_bad = volume_bad = candle_bad = 0
        examples = []
        for e in entries:
            picked = backtest_candle_picker(df, e["_ts"], e["direction"])
            if picked is None:
                continue
            row = df.loc[picked]
            checked += 1

            # What the backtest's candle would have said:
            if e["direction"] == "BUY":
                bt_favor = float(row["close"]) > float(row["open"])
            else:
                bt_favor = float(row["close"]) < float(row["open"])
            pos = df.index.get_loc(picked)
            vol_window = df["tick_volume"].iloc[max(0, pos - (CANDLES_TO_FETCH - 1)):pos + 1]
            bt_low_vol = float(row["tick_volume"]) < float(vol_window.quantile(1 / 3))

            # What the ENGINE actually decided on:
            eng_favor = bool(e["shadow_closed_in_favor"])
            eng_low_vol = bool(e["shadow_low_volume"])
            eng_vol = float(e.get("shadow_tick_volume", 0))

            differs = False
            if bt_favor != eng_favor:
                colour_bad += 1
                differs = True
            if bt_low_vol != eng_low_vol:
                volume_bad += 1
                differs = True
            if abs(float(row["tick_volume"]) - eng_vol) > 0.5:
                candle_bad += 1
                differs = True
            if differs and len(examples) < 4:
                examples.append(
                    f"      entry {e['_ts'].strftime('%m-%d %H:%M:%S')} {e['direction']} @ {e.get('entry')}\n"
                    f"        engine   : candle vol {eng_vol:.0f}, colour {'agreed' if eng_favor else 'DISAGREED'}, "
                    f"low_vol={eng_low_vol}\n"
                    f"        backtest : candle {picked.strftime('%m-%d %H:%M')} vol "
                    f"{float(row['tick_volume']):.0f}, colour {'agreed' if bt_favor else 'DISAGREED'}, "
                    f"low_vol={bt_low_vol}"
                )

        if not checked:
            print(f"{account}: no entries matched.\n")
            continue

        grand_total += checked
        grand_mismatch += candle_bad
        print(f"{'=' * 78}\n{account}: {checked} entries cross-checked\n{'=' * 78}")
        print(f"  WRONG CANDLE picked   : {candle_bad}/{checked} ({100*candle_bad/checked:.0f}%)")
        print(f"  colour disagrees      : {colour_bad}/{checked} ({100*colour_bad/checked:.0f}%)")
        print(f"  volume verdict differs: {volume_bad}/{checked} ({100*volume_bad/checked:.0f}%)")
        if examples:
            print("  examples:")
            for x in examples:
                print(x)
        print()

    if grand_total:
        print(f"{'=' * 78}")
        print(f"OVERALL: the backtest picker chose a different candle than the engine on "
              f"{grand_mismatch}/{grand_total} entries ({100*grand_mismatch/grand_total:.0f}%).")
        print("If that share is high, every conclusion from simulate_demo3_entry_filter.py,")
        print("analyze_entry_quality.py, analyze_trend_filter.py and the other scripts sharing")
        print("this picker was computed on candles the engine never acted on.")


if __name__ == "__main__":
    main()
