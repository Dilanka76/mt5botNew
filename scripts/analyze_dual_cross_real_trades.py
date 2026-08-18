"""Report on REAL closed trades (actual MT5 deal history, not a backtest)
for an account within a time window, broken down by the bot's own
recorded reason for closing each trade — specifically isolating
'validation_failed' trades: dual_cross's tick-based entries that opened
on an unconfirmed provisional EMA13/21 cross and were then force-closed,
regardless of P/L, when that same candle's own close failed to confirm
the cross (Β§4 of the dual_cross spec). This is the "before confirmed the
candle, tick-based entry" issue — every validation_failed close is, by
construction, exactly one of those.

    python scripts/analyze_dual_cross_real_trades.py --account demo1_m1 --from "2026-08-17 00:00" --to "2026-08-18 07:15"
    python scripts/analyze_dual_cross_real_trades.py --account demo1_m3 --from "2026-08-17 00:00" --to "2026-08-18 07:15"

Category meanings (from bot/strategy/state_machine_dual_cross.py):
  - validation_failed: tick-based entry opened, then force-closed at its
    own candle's close because the cross never actually confirmed.
  - stop_loss: the configured $stop_loss_usd was hit.
  - take_profit: the configured $take_profit_usd was hit (or the position
    vanished from the broker and was assumed to be a TP fill).
  - closed_by_concurrent_validation: a second (opposite-direction)
    position's own cross validated, so the original position was closed
    immediately regardless of P/L (Β§5's concurrent-position rule) - not
    a validation failure of its OWN cross, just displaced by the other leg.
  - unknown: a real trade whose close wasn't found in decisions.jsonl for
    this window (e.g. log file rotated/truncated, or the position was
    closed manually) - reported separately, not folded into any category.

Connects to MT5 only to read deal history, then disconnects — never
touches live/demo trading. Reads decisions.jsonl read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.analytics import trade_profit
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, type=validate_account_name)
    parser.add_argument("--from", dest="dt_from", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    parser.add_argument("--to", dest="dt_to", required=True, help='"YYYY-MM-DD HH:MM", UTC')
    return parser.parse_args()


def load_ticket_categories(account: str) -> dict[int, str]:
    """ticket -> close category, from every trade_exited/trade_closed_tp
    line in this account's decisions.jsonl. trade_closed_tp lines never
    carry an explicit category field (see state_machine_dual_cross.py's
    _record_broker_closed) - they're always take_profit by construction."""
    path = Path(f"logs/{account}/decisions.jsonl")
    categories: dict[int, str] = {}
    if not path.exists():
        print(f"WARNING: {path} not found - all trades will show category=unknown", file=sys.stderr)
        return categories
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = entry.get("action")
            ticket = entry.get("ticket")
            if ticket is None:
                continue
            if action == "trade_exited":
                categories[ticket] = entry.get("category", "unknown")
            elif action == "trade_closed_tp":
                categories[ticket] = "take_profit"
    return categories


def longest_streak(results: list[bool]) -> tuple[int, str]:
    """results: True=win, False=loss (breakeven excluded before calling).
    Returns (length, 'W' or 'L') for the single longest streak."""
    if not results:
        return 0, "-"
    best_len, best_kind = 1, results[0]
    cur_len, cur_kind = 1, results[0]
    for r in results[1:]:
        if r == cur_kind:
            cur_len += 1
        else:
            cur_kind, cur_len = r, 1
        if cur_len > best_len:
            best_len, best_kind = cur_len, cur_kind
    return best_len, ("W" if best_kind else "L")


def streak_sequence(results: list[bool]) -> str:
    """Compact run-length view, e.g. [W,W,W,L,L,W,L,L,L,L] -> '3W,2L,1W,4L'."""
    if not results:
        return "-"
    runs = []
    cur_kind, cur_len = results[0], 1
    for r in results[1:]:
        if r == cur_kind:
            cur_len += 1
        else:
            runs.append(f"{cur_len}{'W' if cur_kind else 'L'}")
            cur_kind, cur_len = r, 1
    runs.append(f"{cur_len}{'W' if cur_kind else 'L'}")
    return ",".join(runs)


def analyze(account: str, dt_from: datetime, dt_to: datetime) -> dict:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        deals = mt5.history_deals_get(dt_from, dt_to)
    finally:
        connector.disconnect()

    magic = config.execution.magic_number
    relevant = [d for d in (deals or []) if d.symbol == config.symbol and d.magic == magic]

    by_position: dict[int, list] = {}
    for d in relevant:
        by_position.setdefault(d.position_id, []).append(d)

    categories = load_ticket_categories(account)

    rows = []
    for position_id, deal_list in by_position.items():
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_OUT), None)
        if entry_deal is None or exit_deal is None:
            continue
        profit = trade_profit(exit_deal, entry_deal)
        rows.append({
            "ticket": position_id,
            "direction": "BUY" if entry_deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "entry_price": entry_deal.price,
            "exit_price": exit_deal.price,
            "open_time": datetime.fromtimestamp(entry_deal.time, tz=timezone.utc),
            "close_time": datetime.fromtimestamp(exit_deal.time, tz=timezone.utc),
            "profit": profit,
            "category": categories.get(position_id, "unknown"),
        })
    rows.sort(key=lambda r: r["open_time"])
    return {"account": account, "rows": rows}


def print_report(result: dict) -> None:
    account = result["account"]
    rows = result["rows"]
    print(f"\n{'=' * 70}\nACCOUNT: {account}  ({len(rows)} real closed trades)\n{'=' * 70}")

    if not rows:
        print("No closed trades in this window.")
        return

    total = sum(r["profit"] for r in rows)
    wins = [r for r in rows if r["profit"] > 0]
    losses = [r for r in rows if r["profit"] < 0]
    breakeven = [r for r in rows if r["profit"] == 0]
    win_amt = sum(r["profit"] for r in wins)
    loss_amt = sum(r["profit"] for r in losses)

    print(f"Total trades: {len(rows)}   Wins: {len(wins)}   Losses: {len(losses)}   Breakeven: {len(breakeven)}")
    print(f"Win rate: {len(wins) / len(rows) * 100:.1f}%   Win:Loss ratio = {len(wins)}:{len(losses)}")
    print(f"Total P/L: {total:+.2f}   (profit amount: {win_amt:+.2f}   loss amount: {loss_amt:+.2f})")

    win_flags = [r["profit"] > 0 for r in rows if r["profit"] != 0]
    max_len, max_kind = longest_streak(win_flags)
    print(f"Longest streak: {max_len} consecutive {'wins' if max_kind == 'W' else 'losses'}")
    print(f"Win/Loss sequence (chronological, breakeven excluded): {streak_sequence(win_flags)}")

    print("\nBy close category:")
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_rows in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_total = sum(r["profit"] for r in cat_rows)
        cat_wins = sum(1 for r in cat_rows if r["profit"] > 0)
        cat_losses = sum(1 for r in cat_rows if r["profit"] < 0)
        pct = len(cat_rows) / len(rows) * 100
        flag = "  <-- tick-based entry, cross never confirmed at candle close" if cat == "validation_failed" else ""
        print(
            f"  {cat:<32} {len(cat_rows):>4} trades ({pct:>5.1f}%)  "
            f"wins={cat_wins:<3} losses={cat_losses:<3}  P/L={cat_total:+9.2f}{flag}"
        )

    if "validation_failed" in by_cat:
        vf = by_cat["validation_failed"]
        vf_loss = sum(r["profit"] for r in vf if r["profit"] < 0)
        print(
            f"\nvalidation_failed specifically: {len(vf)} trades, "
            f"{sum(1 for r in vf if r['profit'] < 0)} of them losses, "
            f"totaling {vf_loss:+.2f} lost to unconfirmed tick-based entries "
            f"({abs(vf_loss) / abs(loss_amt) * 100 if loss_amt else 0:.1f}% of all loss $ in this window)"
        )


def main() -> None:
    args = parse_args()
    dt_from = datetime.strptime(args.dt_from, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    dt_to = datetime.strptime(args.dt_to, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    result = analyze(args.account, dt_from, dt_to)
    print_report(result)


if __name__ == "__main__":
    main()
