"""Builds a detailed per-trade Excel workbook from one or more backtest
runs' trades.jsonl output (see scripts/backtest.py) — entry/exit time,
entry type, stop-loss, hit type/close reason, win/loss amount, win/loss
reason, and trade duration, one row per trade, one sheet per account, plus
a Summary sheet.

    python scripts/export_trades_excel.py --accounts demo1_m1,demo1_m3 --from 2026-05-01 --to 2026-08-16

Reads each account's own config for its stop_loss_usd (no hardcoded
value) and expects scripts/backtest.py to have already been run for that
exact account/date-range (reads its trades.jsonl output directly, does
not re-run the backtest itself).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from bot.config import PROJECT_ROOT, load_config, validate_account_name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", required=True, help="Comma-separated account names, e.g. demo1_m1,demo1_m3")
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD, must match the backtest run")
    parser.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD, must match the backtest run")
    parser.add_argument("--out", default=None, help="Output .xlsx path; defaults to reports/backtest/trades_detailed_<from>_<to>.xlsx")
    return parser.parse_args()


def load_trades(path: Path) -> list[dict]:
    trades = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def build_rows(trades: list[dict], stop_loss_usd: float) -> pd.DataFrame:
    rows = []
    for t in trades:
        entry_time = parse_iso(t["open_time"])
        exit_time = parse_iso(t["close_time"])
        duration = exit_time - entry_time
        profit = t["profit"]
        reason = t["reason"]

        if profit > 0:
            outcome, win_amount, loss_amount, win_reason, loss_reason = "WIN", profit, None, reason, None
        elif profit < 0:
            outcome, win_amount, loss_amount, win_reason, loss_reason = "LOSS", None, abs(profit), None, reason
        else:
            outcome, win_amount, loss_amount, win_reason, loss_reason = "BREAKEVEN", None, None, None, reason

        hours, remainder = divmod(int(duration.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        rows.append({
            "Entry Time (UTC)": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Exit Time (UTC)": exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            "Direction": t["direction"],
            "Entry Type": t["entry_type"],
            "Volume (lots)": t["volume"],
            "Entry Price": t["entry_price"],
            "Exit Price": t["price"],
            "Stop Loss ($)": stop_loss_usd,
            "Hit Type / Close Reason": reason,
            "Outcome": outcome,
            "Win Amount ($)": win_amount,
            "Loss Amount ($)": loss_amount,
            "Win Reason": win_reason,
            "Loss Reason": loss_reason,
            "Trade Duration (H:M:S)": duration_str,
            "Duration (minutes)": round(duration.total_seconds() / 60, 1),
            "P/L ($)": profit,
        })
    return pd.DataFrame(rows)


def build_summary(name: str, df: pd.DataFrame) -> dict:
    wins = (df["Outcome"] == "WIN").sum()
    losses = (df["Outcome"] == "LOSS").sum()
    total = len(df)
    avg_win = df.loc[df["Outcome"] == "WIN", "P/L ($)"].mean()
    avg_loss = df.loc[df["Outcome"] == "LOSS", "P/L ($)"].mean()
    return {
        "Account": name,
        "Total Trades": total,
        "Wins": wins,
        "Losses": losses,
        "Win Rate (%)": round(100 * wins / total, 1) if total else 0,
        "Total P/L ($)": round(df["P/L ($)"].sum(), 2),
        "Avg Win ($)": round(avg_win, 2) if wins else 0,
        "Avg Loss ($)": round(avg_loss, 2) if losses else 0,
    }


def build_category_breakdown(name: str, df: pd.DataFrame) -> list[dict]:
    """One row per close-reason category for this account — count, wins,
    losses, and total P/L. Same shape as
    scripts/analyze_dual_cross_real_trades.py's real-trade breakdown, so
    a backtest run and a real-trade run can be compared side by side."""
    rows = []
    total_trades = len(df)
    for category, group in df.groupby("Hit Type / Close Reason"):
        rows.append({
            "Account": name,
            "Category": category,
            "Trades": len(group),
            "% of Trades": round(100 * len(group) / total_trades, 1) if total_trades else 0,
            "Wins": (group["Outcome"] == "WIN").sum(),
            "Losses": (group["Outcome"] == "LOSS").sum(),
            "Total P/L ($)": round(group["P/L ($)"].sum(), 2),
        })
    rows.sort(key=lambda r: -r["Trades"])
    return rows


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


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a.strip()) for a in args.accounts.split(",")]
    stem = f"{args.date_from}_{args.date_to}"
    out_path = Path(args.out) if args.out else PROJECT_ROOT / "reports" / "backtest" / f"trades_detailed_{stem}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summaries = []
    category_rows: list[dict] = []
    all_dfs: list[pd.DataFrame] = []
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for account in accounts:
            config = load_config(account)
            trades_path = PROJECT_ROOT / "reports" / "backtest" / account / f"{stem}.trades.jsonl"
            if not trades_path.exists():
                raise FileNotFoundError(
                    f"{trades_path} not found — run scripts/backtest.py --account {account} "
                    f"--from {args.date_from} --to {args.date_to} first."
                )
            trades = load_trades(trades_path)
            df = build_rows(trades, config.stop_loss_usd or 0.0)
            sheet_name = account[:31]  # Excel sheet name length limit
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            summaries.append(build_summary(account, df))
            category_rows.extend(build_category_breakdown(account, df))
            all_dfs.append(df)
            autofit(writer.sheets[sheet_name], df, 40)

        if len(accounts) > 1:
            combined_df = pd.concat(all_dfs, ignore_index=True)
            summaries.append(build_summary("COMBINED (all accounts)", combined_df))
            category_rows.extend(build_category_breakdown("COMBINED (all accounts)", combined_df))

        summary_df = pd.DataFrame(summaries)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        autofit(writer.sheets["Summary"], summary_df, 30)

        category_df = pd.DataFrame(category_rows)
        category_df.to_excel(writer, sheet_name="By Category", index=False)
        autofit(writer.sheets["By Category"], category_df, 40)

    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
