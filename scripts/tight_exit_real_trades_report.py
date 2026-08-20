"""Comprehensive report on every REAL closed trade (actual MT5 deal
history, not a backtest) placed by dual_cross_tight_exit across BOTH
demo1_m1 and demo1_m3, from a given deployment timestamp through now.
Combines three things this project already has separately:

  1. Real MT5 deal history + trade_profit() -> real P/L per closed trade
     (same technique as analyze_dual_cross_real_trades.py).
  2. Close-reason category per trade, joined from decisions.jsonl (reuses
     analyze_dual_cross_real_trades.py's load_ticket_categories()
     unchanged).
  3. Entry TYPE per trade (tick_cross vs close_confirmed) and whether it
     opened pre-validated — new here. trade_entered lines don't carry a
     ticket, so this pairs each trade_entered with the next ticket-bearing
     event in chronological order (valid because this is a strictly
     single-position engine: entries and exits/validations never overlap).
  4. Structural rule-compliance check (reuses
     verify_tight_exit_rules.check_rule_compliance() unchanged) — flags
     whether every trade in the window actually followed the designed
     rules, not just reports P/L.

    python scripts/tight_exit_real_trades_report.py --accounts demo1_m1,demo1_m3 --since "2026-08-19 05:14:00"

Writes reports/real_trades/<since>_<to>.xlsx: one sheet per account (full
trade list), a Summary sheet (per account + combined), a By Category
sheet, and a By Entry Type sheet. Also prints the full report to the
console. Connects to MT5 only to read deal history, then disconnects —
never touches live/demo trading. Reads decisions.jsonl read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5
import pandas as pd

from bot.analytics import mt5_utc_offset, trade_profit
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_tight_exit_rules import TIMEFRAME_SECONDS, check_rule_compliance  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", default="demo1_m1,demo1_m3", help="Comma-separated account names")
    parser.add_argument("--since", default="2026-08-19 05:14:00", help='"YYYY-MM-DD HH:MM:SS", UTC')
    parser.add_argument("--to", default=None, help='"YYYY-MM-DD HH:MM:SS", UTC — defaults to now')
    parser.add_argument("--out", default=None, help="Output .xlsx path")
    return parser.parse_args()


def load_decision_entries(account: str, since: datetime, to: datetime) -> list[tuple[datetime, dict]]:
    path = Path(f"logs/{account}/decisions.jsonl")
    entries = []
    if not path.exists():
        print(f"WARNING: {path} not found", file=sys.stderr)
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = datetime.fromisoformat(e["timestamp"])
            if since <= ts <= to:
                entries.append((ts, e))
    entries.sort(key=lambda x: x[0])
    return entries


def build_ticket_maps(entries: list[tuple[datetime, dict]]) -> tuple[dict[int, str], dict[int, str], dict[int, bool]]:
    """Returns (ticket->category, ticket->entry_type, ticket->pre_validated).
    entry_type/pre_validated come from the most recent trade_entered's
    reason/flag, attached to the next ticket-bearing event (valid for a
    strictly single-position engine — entries and exits never overlap)."""
    categories: dict[int, str] = {}
    entry_types: dict[int, str] = {}
    pre_validated_map: dict[int, bool] = {}

    pending_entry_type: str | None = None
    pending_pre_validated: bool | None = None

    for ts, e in entries:
        action = e.get("action")
        if action == "trade_entered":
            reason = e.get("reason", "")
            if reason.startswith("tick-cross:"):
                pending_entry_type = "tick_cross"
            elif reason.startswith("close-confirmed (2-candle reversal):"):
                # dual_cross_tight_exit_swap_confirm's actual swap firing
                # (2nd candle reconfirmed) — distinct label from plain
                # close_confirmed so the two are comparable separately,
                # since a swap-triggered entry and a flat fallback entry
                # are genuinely different situations.
                pending_entry_type = "close_confirmed_swap"
            elif reason.startswith("close-confirmed:"):
                pending_entry_type = "close_confirmed"
            else:
                pending_entry_type = "unknown"
            pending_pre_validated = e.get("pre_validated")
        elif action == "trade_exited":
            ticket = e.get("ticket")
            if ticket is not None:
                categories[ticket] = e.get("category", "unknown")
                if pending_entry_type is not None:
                    entry_types[ticket] = pending_entry_type
                    pre_validated_map[ticket] = bool(pending_pre_validated)
        elif action == "trade_closed_tp":
            ticket = e.get("ticket")
            if ticket is not None:
                categories[ticket] = "take_profit"
                if pending_entry_type is not None:
                    entry_types[ticket] = pending_entry_type
                    pre_validated_map[ticket] = bool(pending_pre_validated)

    return categories, entry_types, pre_validated_map


def analyze_account(account: str, since: datetime, to: datetime) -> dict:
    config = load_config(account)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        # MT5's own .time fields (and history_deals_get's query args) use
        # the broker's own time convention, NOT true UTC — confirmed
        # 2026-08-19 to differ from true UTC by a real, exact offset (see
        # bot.analytics.mt5_utc_offset's docstring). Measure it fresh here
        # rather than assume a fixed value.
        offset = mt5_utc_offset(connector, config.symbol)
        deals = mt5.history_deals_get(since + offset, to + offset)
    finally:
        connector.disconnect()

    magic = config.execution.magic_number
    relevant = [d for d in (deals or []) if d.symbol == config.symbol and d.magic == magic]

    by_position: dict[int, list] = {}
    for d in relevant:
        by_position.setdefault(d.position_id, []).append(d)

    entries = load_decision_entries(account, since, to)
    categories, entry_types, pre_validated_map = build_ticket_maps(entries)

    rows = []
    for position_id, deal_list in by_position.items():
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_OUT), None)
        if entry_deal is None or exit_deal is None:
            continue
        open_time = datetime.fromtimestamp(entry_deal.time, tz=timezone.utc) - offset
        close_time = datetime.fromtimestamp(exit_deal.time, tz=timezone.utc) - offset
        if not (since <= open_time <= to):
            continue  # the widened MT5-time query can return deals just outside our true-UTC window
        profit = trade_profit(exit_deal, entry_deal)
        rows.append({
            "account": account,
            "ticket": position_id,
            "direction": "BUY" if entry_deal.type == mt5.DEAL_TYPE_BUY else "SELL",
            "volume": exit_deal.volume,
            "entry_price": entry_deal.price,
            "exit_price": exit_deal.price,
            "open_time": open_time,
            "close_time": close_time,
            "profit": profit,
            "category": categories.get(position_id, "unknown"),
            "entry_type": entry_types.get(position_id, "unknown"),
            "pre_validated": pre_validated_map.get(position_id),
        })
    rows.sort(key=lambda r: r["open_time"])

    candle_seconds = TIMEFRAME_SECONDS.get(config.timeframe, 60)
    violations, counts, final_state = check_rule_compliance(entries, candle_seconds)

    return {
        "account": account, "timeframe": config.timeframe, "rows": rows,
        "violations": violations, "counts": counts, "final_state": final_state,
    }


def longest_streak(results: list[bool]) -> tuple[int, str]:
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


def print_breakdown(title: str, rows: list[dict]) -> None:
    print(f"\n{'=' * 70}\n{title}  ({len(rows)} real closed trades)\n{'=' * 70}")
    if not rows:
        print("No closed trades in this window.")
        return

    total = sum(r["profit"] for r in rows)
    wins = [r for r in rows if r["profit"] > 0]
    losses = [r for r in rows if r["profit"] < 0]
    win_amt = sum(r["profit"] for r in wins)
    loss_amt = sum(r["profit"] for r in losses)

    print(f"Total trades: {len(rows)}   Wins: {len(wins)}   Losses: {len(losses)}")
    print(f"Win rate: {len(wins) / len(rows) * 100:.1f}%   Win:Loss ratio = {len(wins)}:{len(losses)}")
    print(f"Total P/L: {total:+.2f}   (profit amount: {win_amt:+.2f}   loss amount: {loss_amt:+.2f})")

    win_flags = [r["profit"] > 0 for r in rows if r["profit"] != 0]
    max_len, max_kind = longest_streak(win_flags)
    print(f"Longest streak: {max_len} consecutive {'wins' if max_kind == 'W' else 'losses'}")
    print(f"Win/Loss sequence: {streak_sequence(win_flags)}")

    print("\nBy close category:")
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_rows in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_total = sum(r["profit"] for r in cat_rows)
        cat_wins = sum(1 for r in cat_rows if r["profit"] > 0)
        cat_losses = sum(1 for r in cat_rows if r["profit"] < 0)
        pct = len(cat_rows) / len(rows) * 100
        print(f"  {cat:<28} {len(cat_rows):>4} trades ({pct:>5.1f}%)  wins={cat_wins:<3} losses={cat_losses:<3}  P/L={cat_total:+9.2f}")

    print("\nBy entry type:")
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["entry_type"], []).append(r)
    for etype, type_rows in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        type_total = sum(r["profit"] for r in type_rows)
        type_wins = sum(1 for r in type_rows if r["profit"] > 0)
        type_losses = sum(1 for r in type_rows if r["profit"] < 0)
        wr = type_wins / len(type_rows) * 100 if type_rows else 0
        print(f"  {etype:<28} {len(type_rows):>4} trades  win_rate={wr:>5.1f}%  wins={type_wins:<3} losses={type_losses:<3}  P/L={type_total:+9.2f}")


def print_trade_table(rows: list[dict]) -> None:
    print("\nTrade-by-trade (chronological):")
    print(f"  {'account':<10} {'open_time':<20} {'close_time':<20} {'dir':<5} {'entry':>9} {'exit':>9} {'profit':>9}  {'entry_type':<15} category")
    running = 0.0
    for r in rows:
        running += r["profit"]
        outcome = "WIN " if r["profit"] > 0 else ("LOSS" if r["profit"] < 0 else "BE  ")
        print(
            f"  {r['account']:<10} {r['open_time'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
            f"{r['close_time'].strftime('%Y-%m-%d %H:%M:%S'):<20} {r['direction']:<5} "
            f"{r['entry_price']:>9.2f} {r['exit_price']:>9.2f} {outcome} {r['profit']:>+8.2f}  "
            f"{r['entry_type']:<15} {r['category']:<28} running={running:+.2f}"
        )


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a.strip()) for a in args.accounts.split(",")]
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    to = datetime.strptime(args.to, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if args.to else datetime.now(timezone.utc)

    results = [analyze_account(a, since, to) for a in accounts]
    all_rows: list[dict] = []

    for res in results:
        print_breakdown(f"ACCOUNT: {res['account']} ({res['timeframe']})", res["rows"])
        all_rows.extend(res["rows"])

        print(f"\nRule compliance for {res['account']}:")
        for k, v in res["counts"].items():
            print(f"  {k:<28} {v}")
        if res["violations"]:
            print(f"  {len(res['violations'])} RULE VIOLATION(S):")
            for ts, msg in res["violations"]:
                print(f"    [{ts.isoformat()}] {msg}")
        else:
            print("  No rule violations found.")

    if len(accounts) > 1:
        print_breakdown("COMBINED (all accounts)", all_rows)

    print_trade_table(all_rows)

    # --- Excel export ---
    stem = f"{args.since.replace(':', '').replace(' ', '_')}_{(args.to or 'now').replace(':', '').replace(' ', '_')}"
    out_path = Path(args.out) if args.out else PROJECT_ROOT / "reports" / "real_trades" / f"{stem}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _cell_width(value) -> int:
        if value is None:
            return 0
        try:
            if pd.isna(value):
                return 0
        except (TypeError, ValueError):
            pass
        return len(str(value))

    def autofit(worksheet, df: pd.DataFrame, max_width: int) -> None:
        for i, col in enumerate(df.columns, start=1):
            col_max = df[col].map(_cell_width).max()
            width = max(int(col_max) if pd.notna(col_max) else 0, len(col)) + 2
            worksheet.column_dimensions[worksheet.cell(row=1, column=i).column_letter].width = min(width, max_width)

    def rows_to_df(rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame([{
            "Open Time (UTC)": r["open_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "Close Time (UTC)": r["close_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "Direction": r["direction"],
            "Volume (lots)": r["volume"],
            "Entry Price": r["entry_price"],
            "Exit Price": r["exit_price"],
            "Entry Type": r["entry_type"],
            "Pre-Validated at Entry": r["pre_validated"],
            "Close Category": r["category"],
            "Outcome": "WIN" if r["profit"] > 0 else ("LOSS" if r["profit"] < 0 else "BREAKEVEN"),
            "P/L ($)": round(r["profit"], 2),
            "Ticket": r["ticket"],
        } for r in rows])

    def summary_row(name: str, rows: list[dict]) -> dict:
        total = len(rows)
        wins = sum(1 for r in rows if r["profit"] > 0)
        losses = sum(1 for r in rows if r["profit"] < 0)
        win_amt = sum(r["profit"] for r in rows if r["profit"] > 0)
        loss_amt = sum(r["profit"] for r in rows if r["profit"] < 0)
        return {
            "Account": name, "Total Trades": total, "Wins": wins, "Losses": losses,
            "Win Rate (%)": round(100 * wins / total, 1) if total else 0,
            "Total P/L ($)": round(sum(r["profit"] for r in rows), 2),
            "Avg Win ($)": round(win_amt / wins, 2) if wins else 0,
            "Avg Loss ($)": round(loss_amt / losses, 2) if losses else 0,
        }

    def category_rows(name: str, rows: list[dict]) -> list[dict]:
        by_cat: dict[str, list] = {}
        for r in rows:
            by_cat.setdefault(r["category"], []).append(r)
        out = []
        for cat, cat_rows in by_cat.items():
            out.append({
                "Account": name, "Category": cat, "Trades": len(cat_rows),
                "Wins": sum(1 for r in cat_rows if r["profit"] > 0),
                "Losses": sum(1 for r in cat_rows if r["profit"] < 0),
                "Total P/L ($)": round(sum(r["profit"] for r in cat_rows), 2),
            })
        return out

    def entry_type_rows(name: str, rows: list[dict]) -> list[dict]:
        by_type: dict[str, list] = {}
        for r in rows:
            by_type.setdefault(r["entry_type"], []).append(r)
        out = []
        for etype, type_rows in by_type.items():
            out.append({
                "Account": name, "Entry Type": etype, "Trades": len(type_rows),
                "Wins": sum(1 for r in type_rows if r["profit"] > 0),
                "Losses": sum(1 for r in type_rows if r["profit"] < 0),
                "Win Rate (%)": round(100 * sum(1 for r in type_rows if r["profit"] > 0) / len(type_rows), 1) if type_rows else 0,
                "Total P/L ($)": round(sum(r["profit"] for r in type_rows), 2),
            })
        return out

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for res in results:
            df = rows_to_df(res["rows"])
            sheet_name = res["account"][:31]
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            if len(df):
                autofit(writer.sheets[sheet_name], df, 40)

        summaries = [summary_row(res["account"], res["rows"]) for res in results]
        if len(accounts) > 1:
            summaries.append(summary_row("COMBINED (all accounts)", all_rows))
        summary_df = pd.DataFrame(summaries)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        autofit(writer.sheets["Summary"], summary_df, 30)

        cat_rows_all = []
        for res in results:
            cat_rows_all.extend(category_rows(res["account"], res["rows"]))
        if len(accounts) > 1:
            cat_rows_all.extend(category_rows("COMBINED (all accounts)", all_rows))
        cat_df = pd.DataFrame(cat_rows_all)
        cat_df.to_excel(writer, sheet_name="By Category", index=False)
        if len(cat_df):
            autofit(writer.sheets["By Category"], cat_df, 40)

        et_rows_all = []
        for res in results:
            et_rows_all.extend(entry_type_rows(res["account"], res["rows"]))
        if len(accounts) > 1:
            et_rows_all.extend(entry_type_rows("COMBINED (all accounts)", all_rows))
        et_df = pd.DataFrame(et_rows_all)
        et_df.to_excel(writer, sheet_name="By Entry Type", index=False)
        if len(et_df):
            autofit(writer.sheets["By Entry Type"], et_df, 40)

    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    main()
