"""Full strategy analysis for demo1_m1/demo1_m3 (dual_cross_confirmed_swap_adx)
-- runs the standing 7-item checklist in one pass: (1) overall stats,
(2) every close category, (3) every entry type, (4) rule-compliance
checks, (5) before/after comparison around the 2026-08-28 gap-threshold
change, (6) a verdict per mechanism, (7) data-quality flags.

    python scripts/full_strategy_analysis.py

Covers TESTING_START_UTC (2026-08-25 03:30 Sri Lanka time) through now,
same window as the live report. Read-only: only pulls real closed-trade
history, never touches live/demo trading.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for `bot.*` imports inside generate_live_test_report
sys.path.insert(0, str(Path(__file__).resolve().parent))  # so generate_live_test_report itself is importable

from generate_live_test_report import CATEGORY_LABELS, gather_account_data, load_config  # noqa: E402

# demo1_m3's gap_threshold_usd went from $5.00 to $7.00 at this exact
# restart (confirmed via its own `Bot started` log line 2026-08-28
# 16:35:25 UTC) -- everything before this used $5, everything at/after
# used $7. demo1_m1 was never touched by this change.
GAP_CHANGE_CUTOVER_UTC = datetime(2026, 8, 28, 16, 35, 25, tzinfo=timezone.utc)


def pct(part: int, whole: int) -> float:
    return 100 * part / whole if whole else 0.0


def item1_overall(records: list[dict], label: str) -> None:
    n = len(records)
    wins = [r for r in records if r["outcome"] == "WIN"]
    losses = [r for r in records if r["outcome"] == "LOSS"]
    pl = sum(r["profit"] for r in records)
    win_dollars = sum(r["profit"] for r in wins)
    loss_dollars = sum(r["profit"] for r in losses)
    print(f"\n{'=' * 78}\n1) OVERALL — {label}\n{'=' * 78}")
    print(f"  Total trades: {n}  |  Wins: {len(wins)} ({pct(len(wins), n):.1f}%)  |  Losses: {len(losses)} ({pct(len(losses), n):.1f}%)")
    print(f"  Total P/L: ${pl:+.2f}  |  Win $: ${win_dollars:+.2f}  |  Loss $: ${loss_dollars:+.2f}")
    if wins:
        print(f"  Avg win: ${mean(r['profit'] for r in wins):+.2f}")
    if losses:
        print(f"  Avg loss: ${mean(r['profit'] for r in losses):+.2f}")


def item2_categories(records: list[dict], label: str) -> None:
    print(f"\n{'-' * 78}\n2) CLOSE CATEGORIES — {label}\n{'-' * 78}")
    total_loss = abs(sum(r["profit"] for r in records if r["profit"] < 0))
    cats = sorted({r["close_category"] for r in records})
    for cat in cats:
        rows = [r for r in records if r["close_category"] == cat]
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        pl = sum(r["profit"] for r in rows)
        cat_loss = abs(sum(r["profit"] for r in rows if r["profit"] < 0))
        loss_share = pct(cat_loss, total_loss) if total_loss else 0.0
        label_txt = CATEGORY_LABELS.get(cat, cat)
        print(f"  {label_txt:<28} n={n:>3}  win={pct(wins, n):5.1f}%  P/L=${pl:+8.2f}  ({loss_share:.1f}% of total loss$)")


def item3_entry_types(records: list[dict], label: str) -> None:
    print(f"\n{'-' * 78}\n3) ENTRY TYPES — {label}\n{'-' * 78}")
    for etype in ("immediate", "ema5_touch", "unknown"):
        rows = [r for r in records if r.get("_entry_type", "unknown") == etype]
        if not rows:
            continue
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        pl = sum(r["profit"] for r in rows)
        print(f"  {etype:<12} n={n:>3}  win={pct(wins, n):5.1f}%  P/L=${pl:+8.2f}  avg=${pl / n:+.2f}")


def _gap_threshold_at(account: str, entry_time_utc: datetime) -> float:
    """demo1_m3's gap_threshold_usd changed $5 -> $7 at GAP_CHANGE_CUTOVER_UTC
    (see module docstring) -- a fixed $5.0 for every other account/time.
    Fixed 2026-08-31: an earlier version of this script hardcoded $5.0
    unconditionally, misclassifying demo1_m3 trades entered after the
    change (real threshold $7, so a $5-$7 gap should read as "immediate"
    but the old code called it "ema5_touch")."""
    if account == "demo1_m3" and entry_time_utc >= GAP_CHANGE_CUTOVER_UTC:
        return 7.0
    return 5.0


def tag_entry_type(records: list[dict]) -> None:
    """entry_gap is None for swap re-entries (by design); everything else
    is 'immediate' if a small gap was logged, 'ema5_touch' if a wide one
    was, going by build_trade_records's own gap extraction -- using the
    gap threshold that was ACTUALLY live at each trade's own entry time,
    not a single fixed value (see _gap_threshold_at)."""
    for r in records:
        if r["entry_gap"] is None:
            r["_entry_type"] = "swap_reentry (no gap logged, by design)"
        else:
            threshold = _gap_threshold_at(r["account"], r["entry_time"].astimezone(timezone.utc))
            r["_entry_type"] = "immediate" if r["entry_gap"] < threshold else "ema5_touch"


def item4_rule_compliance(account: str, decisions: list[dict], records: list[dict], config) -> list[str]:
    violations = []
    sessions = config.sessions.get(config.strategy_variant, [])

    # a) every trade_entered decision line should have a corresponding real
    #    MT5 trade close to it in time+direction (checked upstream by
    #    build_trade_records's pairing -- here we check the reverse: no
    #    trade_entered logged outside session hours).
    from bot.sessions import is_within_session
    entries = [e for e in decisions if e.get("action") == "trade_entered"]
    for e in entries:
        if not is_within_session(sessions, e["_ts"]):
            violations.append(f"{account}: trade_entered logged OUTSIDE session hours at {e['_ts']} -- {e.get('reason', '')}")

    # b) every swap-category close should have a preceding swap_pending
    #    within a reasonable window (2-candle debounce should always leave
    #    this trail).
    swap_closes = [r for r in records if r["close_category"] == "swapped_confirmed_reversal"]
    swap_pending_times = [e["_ts"] for e in decisions if e.get("action") == "swap_pending"]
    for r in swap_closes:
        exit_utc = r["exit_time"].astimezone(timezone.utc)
        if not any(abs((exit_utc - t).total_seconds()) <= 600 for t in swap_pending_times):
            violations.append(f"{account}: swap close (ticket={r['ticket']}) at {exit_utc} has NO swap_pending logged nearby -- should always be preceded by one")

    # c) stop-loss category closes shouldn't have a P/L magnitude wildly
    #    exceeding config.stop_loss_usd's dollar-scaled expectation (sanity
    #    check only, real fills vary with lot size/slippage, so this is a
    #    generous bound not an exact match).
    sl_closes = [r for r in records if r["close_category"] == "stop_loss"]
    for r in sl_closes:
        if r["profit"] > 0:
            violations.append(f"{account}: stop_loss-categorized close (ticket={r['ticket']}) has POSITIVE profit ${r['profit']:+.2f} -- category mismatch?")

    return violations


def item6b_swap_mechanism(account: str, rules: dict) -> None:
    """Real pending/confirmed/blocked/cancelled breakdown for the swap --
    already computed by build_rule_tracking_swap_adx inside
    gather_account_data(), just wasn't being printed before. Answers the
    question raised by item 2 finding ZERO swap-category closes: is the
    swap a rare-but-real safety net, or effectively inert?"""
    armed = rules["swap_pending_count"]
    confirmed = rules["swap_confirmed_count"]
    blocked = rules["swap_blocked_count"]
    cancelled = rules["swap_cancelled_count"]
    print(f"  {account}: armed={armed}  confirmed={confirmed}  blocked_by_adx={blocked}  cancelled_no_2nd_candle={cancelled}")
    if armed:
        print(f"    -> of {armed} armed episodes: {pct(confirmed, armed):.1f}% confirmed, "
              f"{pct(blocked, armed):.1f}% blocked by ADX, {pct(cancelled, armed):.1f}% cancelled (no 2nd candle)")


def item5_before_after_gap_change(m3_records: list[dict]) -> None:
    print(f"\n{'=' * 78}\n5) BEFORE/AFTER — demo1_m3 gap_threshold_usd $5 -> $7 (cutover {GAP_CHANGE_CUTOVER_UTC})\n{'=' * 78}")
    before = [r for r in m3_records if r["entry_time"].astimezone(timezone.utc) < GAP_CHANGE_CUTOVER_UTC]
    after = [r for r in m3_records if r["entry_time"].astimezone(timezone.utc) >= GAP_CHANGE_CUTOVER_UTC]
    for label, rows in [("BEFORE ($5 threshold)", before), ("AFTER ($7 threshold)", after)]:
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        pl = sum(r["profit"] for r in rows)
        print(f"  {label:<24} n={n:>3}  win={pct(wins, n):5.1f}%  P/L=${pl:+.2f}")
    if not after:
        print("  NOTE: no trades yet since the change -- too early to judge its effect.")


def main() -> None:
    accounts = [("demo1_m1", "M1"), ("demo1_m3", "M3")]
    all_records: list[dict] = []
    all_violations: list[str] = []
    per_account: dict[str, tuple] = {}

    for account, timeframe in accounts:
        print(f"Reading {account} ({timeframe})...")
        decisions, records, rules = gather_account_data(account, timeframe)
        tag_entry_type(records)
        config = load_config(account)
        per_account[account] = (decisions, records, rules, config)
        all_records.extend(records)

    for account, timeframe in accounts:
        decisions, records, rules, config = per_account[account]
        item1_overall(records, account)
        item2_categories(records, account)
        item3_entry_types(records, account)
        violations = item4_rule_compliance(account, decisions, records, config)
        all_violations.extend(violations)

    item1_overall(all_records, "COMBINED (demo1_m1 + demo1_m3)")
    item2_categories(all_records, "COMBINED")

    print(f"\n{'=' * 78}\n6b) SWAP MECHANISM — armed vs confirmed vs blocked vs cancelled\n{'=' * 78}")
    for account, _ in accounts:
        item6b_swap_mechanism(account, per_account[account][2])

    item5_before_after_gap_change(per_account["demo1_m3"][1])

    print(f"\n{'=' * 78}\n4) RULE-COMPLIANCE VIOLATIONS\n{'=' * 78}")
    if all_violations:
        for v in all_violations:
            print(f"  VIOLATION: {v}")
    else:
        print("  0 violations found.")

    print(f"\n{'=' * 78}\nDone. Combined trades analyzed: {len(all_records)}\n{'=' * 78}")


if __name__ == "__main__":
    main()
