"""Verifies every dual_cross_tight_exit trade since a given deployment
timestamp actually followed the exact rules we designed — not a spot
check, a structural walk through every decisions.jsonl entry checking
each state transition against what the engine is supposed to do:

  - trade_entered is either "tick-cross" (pre_validated=false) or
    "close-confirmed" (pre_validated=true) — nothing else.
  - A position only ever exists in one of two states: UNVALIDATED (just
    opened via tick-cross, own candle hasn't closed yet) or VALIDATED
    (either opened pre-validated, or survived its own candle's close).
  - early_exit_unconfirmed can ONLY happen to an UNVALIDATED position.
  - stop_loss can ONLY happen to a VALIDATED position (never before
    confirmation — this is the exact fix from 2026-08-19).
  - swapped_confirmed_reversal can ONLY happen to a VALIDATED position
    (structurally impossible to happen to a same-candle unvalidated one —
    see state_machine_dual_cross_tight_exit.py's on_new_candle: its own
    validation check always resolves a position before the swap check
    runs later in the same call).
  - take_profit may happen in either state (allowed by design).
  - No tick-cross entry may directly follow an early_exit_unconfirmed
    close within roughly one candle's duration (the one-attempt-per-candle
    rule) — the next entry in that window must be close-confirmed.

    python scripts/verify_tight_exit_rules.py --account demo1_m1 --since "2026-08-19 05:14:00"
    python scripts/verify_tight_exit_rules.py --account demo1_m3 --since "2026-08-19 05:14:00"

Reads decisions.jsonl only — no MT5 connection needed except to load the
account's config (for its timeframe, used for the one-attempt-per-candle
window).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config, validate_account_name

TIMEFRAME_SECONDS = {"M1": 60, "M3": 180, "M5": 300, "M15": 900}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", UTC — the deployment timestamp')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.account)
    if config.strategy_variant != "dual_cross_tight_exit":
        print(f"WARNING: {args.account}'s current strategy_variant is '{config.strategy_variant}', not "
              f"dual_cross_tight_exit — rules below won't apply to older entries in this log.")
    candle_seconds = TIMEFRAME_SECONDS.get(config.timeframe, 60)

    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    path = Path(f"logs/{args.account}/decisions.jsonl")
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") not in ("trade_entered", "position_validated", "trade_exited", "trade_closed_tp"):
                continue
            ts = datetime.fromisoformat(e["timestamp"])
            if ts < since:
                continue
            entries.append((ts, e))

    entries.sort(key=lambda x: x[0])
    print(f"account={args.account} timeframe={config.timeframe} ({candle_seconds}s/candle) since={args.since}")
    print(f"{len(entries)} relevant decision-log entries found.\n")

    violations, counts, state = check_rule_compliance(entries, candle_seconds)

    print("Event counts:")
    for k, v in counts.items():
        print(f"  {k:<28} {v}")

    print(f"\nFinal state at end of log window: {state}")
    print()
    if violations:
        print(f"{len(violations)} RULE VIOLATION(S) FOUND:\n")
        for ts, msg in violations:
            print(f"  [{ts.isoformat()}] {msg}")
        sys.exit(1)
    else:
        print("No rule violations found — every trade in this window followed the designed rules exactly.")


def check_rule_compliance(
    entries: list[tuple[datetime, dict]], candle_seconds: int,
) -> tuple[list[tuple[datetime, str]], dict[str, int], str]:
    """Walks a chronological list of (timestamp, decision-log-entry) pairs
    through the exact dual_cross_tight_exit state machine
    (NONE/UNVALIDATED/VALIDATED), flagging any transition that shouldn't be
    possible per the designed rules. Returns (violations, event_counts,
    final_state). Importable — used by both this script's CLI and
    scripts/tight_exit_real_trades_report.py."""
    violations: list[tuple[datetime, str]] = []
    state = "NONE"  # NONE | UNVALIDATED | VALIDATED
    last_early_exit_time: datetime | None = None
    counts = {"trade_entered_tick": 0, "trade_entered_fallback": 0, "position_validated": 0,
              "early_exit_unconfirmed": 0, "validation_failed": 0, "stop_loss": 0,
              "swapped_confirmed_reversal": 0, "take_profit": 0}

    for ts, e in entries:
        action = e.get("action")
        reason = e.get("reason", "")
        category = e.get("category")

        if action == "trade_entered":
            is_tick = reason.startswith("tick-cross:")
            # "close-confirmed:" (dual_cross_tight_exit, and flat entries on
            # dual_cross_tight_exit_swap_confirm), "close-confirmed (2-candle
            # reversal):" (a swap-confirm reversal that actually fired), or
            # "EMA5 touch at ... for pending ... setup (close-confirmed: ...)"
            # (dual_cross_confirmed_swap_adx's gap/EMA5-pullback entry,
            # 2026-08-21 — the ORIGINAL close-confirmed reason text is
            # embedded inside this one, just wrapped) — all are legitimate
            # pre_validated=True entries, just worded differently depending
            # on which engine/path produced them.
            is_fallback = reason.startswith("close-confirmed") or reason.startswith("EMA5 touch at")
            if not is_tick and not is_fallback:
                violations.append((ts, f"UNEXPECTED entry reason format: {reason!r}"))
            if state != "NONE":
                violations.append((ts, f"trade_entered while state was {state} (should only enter from NONE)"))

            if is_tick and last_early_exit_time is not None:
                gap = (ts - last_early_exit_time).total_seconds()
                if gap < candle_seconds:
                    violations.append((
                        ts,
                        f"ONE-ATTEMPT-PER-CANDLE VIOLATION: tick-cross entry only {gap:.0f}s after an "
                        f"early_exit_unconfirmed close ({candle_seconds}s candle) — should have been "
                        f"close-confirmed only",
                    ))

            counts["trade_entered_tick" if is_tick else "trade_entered_fallback"] += 1
            state = "UNVALIDATED" if is_tick else "VALIDATED"
            last_early_exit_time = None  # a fresh entry supersedes the "just had an early exit" window

        elif action == "position_validated":
            if state != "UNVALIDATED":
                violations.append((ts, f"position_validated while state was {state} (expected UNVALIDATED)"))
            counts["position_validated"] += 1
            state = "VALIDATED"

        elif action == "trade_exited":
            if category == "early_exit_unconfirmed":
                if state != "UNVALIDATED":
                    violations.append((ts, f"early_exit_unconfirmed while state was {state} — should ONLY hit an UNVALIDATED position"))
                counts["early_exit_unconfirmed"] += 1
                last_early_exit_time = ts
                state = "NONE"
            elif category == "validation_failed":
                if state != "UNVALIDATED":
                    violations.append((ts, f"validation_failed while state was {state} (expected UNVALIDATED)"))
                counts["validation_failed"] += 1
                state = "NONE"
            elif category == "stop_loss":
                if state != "VALIDATED":
                    violations.append((ts, f"STOP-LOSS/EARLY-EXIT MIX-UP: stop_loss fired while state was {state} — the $15 stop must ONLY ever apply to a VALIDATED position"))
                counts["stop_loss"] += 1
                state = "NONE"
            elif category == "swapped_confirmed_reversal":
                if state != "VALIDATED":
                    violations.append((ts, f"swapped_confirmed_reversal while state was {state} — this should be structurally impossible on an UNVALIDATED position"))
                counts["swapped_confirmed_reversal"] += 1
                state = "NONE"
            else:
                violations.append((ts, f"UNEXPECTED close category for this engine: {category!r}"))
                state = "NONE"

        elif action == "trade_closed_tp":
            counts["take_profit"] += 1
            state = "NONE"

    return violations, counts, state


if __name__ == "__main__":
    main()
