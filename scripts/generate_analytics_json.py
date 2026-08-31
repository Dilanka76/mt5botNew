"""Writes reports/analytics/<account>/latest.json for each of the four
accounts (demo1_m1, demo1_m3, demo2_m1, demo2_m3) -- the JSON backing for
api_server.py's new GET /{account}/analytics/full route (see
mt5botc_controll's web dashboard).

Reuses the exact same computations already validated in
full_strategy_analysis.py (overall stats, close categories, entry types,
rule-compliance violations, swap-mechanism breakdown for the ADX-gated
variant) -- this script just returns/serializes them as JSON instead of
printing, and generalizes to all four accounts instead of demo1 only.

    python scripts/generate_analytics_json.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3 --since "2026-08-25 00:00:00"

Intended to run periodically via a Scheduled Task (same 15-min/S4U pattern
as MT5-Report-Generator) -- the gateway (api_server.py) NEVER computes this
itself, only reads the JSON this script writes (see api_server.py's module
docstring: it must never call MetaTrader5 directly).

Read-only: only pulls real closed-trade history, never touches live/demo
trading.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so sibling scripts are importable

from bot.config import PROJECT_ROOT  # noqa: E402
from full_strategy_analysis import GAP_CHANGE_CUTOVER_UTC, item4_rule_compliance  # noqa: E402
from generate_live_test_report import CATEGORY_LABELS, gather_account_data, load_config  # noqa: E402


def tag_entry_type(records: list[dict], config) -> None:
    """Unlike full_strategy_analysis.py's tag_entry_type(), this uses each
    account's OWN real config.gap_threshold_usd (demo1_m1=$5, demo1_m3=$7
    now, demo2_m1=$5, demo2_m3=$7) rather than a hardcoded default of $5 --
    that hardcoded version only special-cases demo1_m3's historical $5->$7
    change and would silently misclassify demo2_m3's entries (real
    threshold $7, not $5) the exact same way demo1_m3's were mistakenly
    classified before that bug was fixed. demo1_m3 still needs the time-
    aware split since ITS threshold changed mid-window; every other
    account's threshold has been constant for the whole tracked period."""
    for r in records:
        if r["entry_gap"] is None:
            r["_entry_type"] = "swap_reentry"
            continue
        if r["account"] == "demo1_m3":
            threshold = 7.0 if r["entry_time"].astimezone(timezone.utc) >= GAP_CHANGE_CUTOVER_UTC else 5.0
        else:
            threshold = config.gap_threshold_usd
        r["_entry_type"] = "immediate" if r["entry_gap"] < threshold else "ema5_touch"


def pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def build_overall(records: list[dict]) -> dict:
    n = len(records)
    wins = [r for r in records if r["outcome"] == "WIN"]
    losses = [r for r in records if r["outcome"] == "LOSS"]
    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": pct(len(wins), n),
        "total_pl": round(sum(r["profit"] for r in records), 2),
        "win_dollars": round(sum(r["profit"] for r in wins), 2),
        "loss_dollars": round(sum(r["profit"] for r in losses), 2),
        "avg_win": round(mean(r["profit"] for r in wins), 2) if wins else None,
        "avg_loss": round(mean(r["profit"] for r in losses), 2) if losses else None,
    }


def build_categories(records: list[dict]) -> list[dict]:
    total_loss = abs(sum(r["profit"] for r in records if r["profit"] < 0))
    out = []
    for cat in sorted({r["close_category"] for r in records}):
        rows = [r for r in records if r["close_category"] == cat]
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        cat_loss = abs(sum(r["profit"] for r in rows if r["profit"] < 0))
        out.append({
            "category": cat,
            "label": CATEGORY_LABELS.get(cat, cat),
            "trades": n,
            "win_rate_pct": pct(wins, n),
            "pl": round(sum(r["profit"] for r in rows), 2),
            "pct_of_total_loss": pct(cat_loss, total_loss) if total_loss else 0.0,
        })
    return out


def build_entry_types(records: list[dict]) -> list[dict]:
    out = []
    for etype in ("immediate", "ema5_touch"):
        rows = [r for r in records if r.get("_entry_type") == etype]
        if not rows:
            continue
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        pl = sum(r["profit"] for r in rows)
        out.append({
            "entry_type": etype,
            "trades": n,
            "win_rate_pct": pct(wins, n),
            "pl": round(pl, 2),
            "avg_pl": round(pl / n, 2),
        })
    return out


def build_swap_mechanism(rules: dict) -> dict | None:
    if "swap_pending_count" not in rules:
        return None  # simple_swap variant (demo2) has no debounce/ADX gate to report on
    armed = rules["swap_pending_count"]
    return {
        "armed": armed,
        "confirmed": rules["swap_confirmed_count"],
        "blocked_by_adx": rules["swap_blocked_count"],
        "cancelled_no_2nd_candle": rules["swap_cancelled_count"],
        "confirmed_pct": pct(rules["swap_confirmed_count"], armed),
        "blocked_pct": pct(rules["swap_blocked_count"], armed),
        "cancelled_pct": pct(rules["swap_cancelled_count"], armed),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    accounts = [a.strip() for a in args.accounts.split(",")]

    for account in accounts:
        timeframe = "M1" if account.endswith("_m1") else "M3"
        print(f"Generating analytics JSON for {account} ({timeframe})...")
        config = load_config(account)
        decisions, records, rules = gather_account_data(account, timeframe)
        tag_entry_type(records, config)
        violations = item4_rule_compliance(account, decisions, records, config)

        payload = {
            "account": account,
            "timeframe": timeframe,
            "strategy_variant": config.strategy_variant,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "since": args.since,
            "overall": build_overall(records),
            "categories": build_categories(records),
            "entry_types": build_entry_types(records),
            "rule_compliance": {
                "violation_count": len(violations),
                "violations": violations,
            },
            "swap_mechanism": build_swap_mechanism(rules),
        }

        out_dir = PROJECT_ROOT / "reports" / "analytics" / account
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "latest.json"
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"  Written: {out_path} ({payload['overall']['total_trades']} trades)")


if __name__ == "__main__":
    main()
