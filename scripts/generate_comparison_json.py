"""Writes reports/analytics/comparison/latest.json -- the JSON backing for
api_server.py's new GET /comparison route (see mt5botc_controll's web
dashboard's demo1-vs-demo2 comparison view).

Reuses diff_trades_today.py's trade-by-trade match-up (matched / demo2-only
/ demo1-only, with a logged-reason explanation where one exists) -- same
real methodology as the standing project_demo1_demo2_comparison_log
memory, just serialized to JSON instead of printed.

    python scripts/generate_comparison_json.py

Intended to run periodically via a Scheduled Task (same pattern as the
other analytics/report generators) -- the gateway never computes this
itself, only reads the JSON this script writes.

Read-only: only pulls real closed-trade history, never touches live/demo
trading.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so sibling scripts are importable

from bot.config import PROJECT_ROOT  # noqa: E402
from diff_trades_today import LEG_PAIRS, find_nearby_explanation, match_trades  # noqa: E402
from generate_live_test_report import gather_account_data, trading_day  # noqa: E402


def trade_summary(r: dict) -> dict:
    return {
        "entry_time_utc": r["entry_time"].astimezone(timezone.utc).isoformat(),
        "direction": r["direction"],
        "entry_price": r["entry_price"],
        "exit_price": r["exit_price"],
        "profit": round(r["profit"], 2),
        "outcome": r["outcome"],
        "close_category": r["close_category"],
    }


def build_leg_comparison(demo1_account: str, demo2_account: str, timeframe: str, window, today) -> dict:
    d1_decisions, d1_records_all, _ = gather_account_data(demo1_account, timeframe)
    d2_decisions, d2_records_all, _ = gather_account_data(demo2_account, timeframe)
    d1_records = [r for r in d1_records_all if trading_day(r["exit_time"]) == today]
    d2_records = [r for r in d2_records_all if trading_day(r["exit_time"]) == today]

    matched, demo1_only, demo2_only = match_trades(d1_records, d2_records, window)

    demo2_only_out = []
    for r in demo2_only:
        explanation = find_nearby_explanation(d1_decisions, r["entry_time"], r["direction"], window)
        demo2_only_out.append({**trade_summary(r), "demo1_reason": explanation})

    demo1_only_out = []
    for r in demo1_only:
        explanation = find_nearby_explanation(d2_decisions, r["entry_time"], r["direction"], window)
        demo1_only_out.append({**trade_summary(r), "demo2_reason": explanation})

    return {
        "demo1_account": demo1_account,
        "demo2_account": demo2_account,
        "timeframe": timeframe,
        "matched_count": len(matched),
        "demo2_only": demo2_only_out,
        "demo2_only_net_pl": round(sum(r["profit"] for r in demo2_only), 2),
        "demo1_only": demo1_only_out,
        "demo1_only_net_pl": round(sum(r["profit"] for r in demo1_only), 2),
        "demo1_today_totals": {
            "trades": len(d1_records),
            "win_rate_pct": round(100 * sum(1 for r in d1_records if r["outcome"] == "WIN") / len(d1_records), 1) if d1_records else 0.0,
            "pl": round(sum(r["profit"] for r in d1_records), 2),
        },
        "demo2_today_totals": {
            "trades": len(d2_records),
            "win_rate_pct": round(100 * sum(1 for r in d2_records if r["outcome"] == "WIN") / len(d2_records), 1) if d2_records else 0.0,
            "pl": round(sum(r["profit"] for r in d2_records), 2),
        },
    }


def main() -> None:
    today = trading_day(datetime.now(timezone.utc))
    legs = []
    for demo1_account, demo2_account, timeframe, window in LEG_PAIRS:
        print(f"Comparing {demo1_account} vs {demo2_account} ({timeframe})...")
        legs.append(build_leg_comparison(demo1_account, demo2_account, timeframe, window, today))

    payload = {
        "trading_day": today.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "legs": legs,
    }

    out_dir = PROJECT_ROOT / "reports" / "analytics" / "comparison"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "latest.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
