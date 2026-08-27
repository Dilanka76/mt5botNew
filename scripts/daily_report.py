"""Generates a markdown daily trading report from REAL MT5 trade history —
not a backtest. Read-only: connects to MT5 to query account info and trade
history only. Never places, modifies, or closes a trade.

Usage:
    python scripts/daily_report.py --account demo1                    # today (Asia/Colombo calendar day)
    python scripts/daily_report.py --account demo1 --date 2026-08-03  # a specific past day

Designed to also run unattended once a day via a Windows Scheduled Task —
one task per account, each passing that account's --account.

Writes reports/daily/<account>/<date>.md.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.analytics import COLOMBO, day_bounds_utc, get_balance_at, get_closed_trades, mt5_utc_offset
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector
from bot.trade_stats import compute_day_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily trading report from real MT5 trade history.")
    parser.add_argument(
        "--account", required=True, type=validate_account_name,
        help="Account name, e.g. demo1, live1. Selects .env.<account>/config/settings.<account>.yaml.",
    )
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (Asia/Colombo calendar day). Defaults to today.")
    args = parser.parse_args()
    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            parser.error(f"--date must be YYYY-MM-DD, got {args.date!r}")
    return args


def build_report_markdown(
    target_date, trades: list[dict], start_balance: float, end_balance: float,
    current_equity: float, symbol: str,
) -> str:
    stats = compute_day_stats(trades)
    total, win_rate, total_pl = stats["total_trades"], stats["win_rate"], stats["total_pl"]
    avg_win, avg_loss = stats["avg_win"], stats["avg_loss"]

    lines = [
        f"# Daily Trading Report — {target_date.isoformat()}",
        "",
        f"Symbol: `{symbol}` | Generated: {datetime.now(COLOMBO).strftime('%Y-%m-%d %H:%M:%S')} (Asia/Colombo)",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Total trades | {total} |",
        f"| Wins / Losses / Breakeven | {stats['wins']} / {stats['losses']} / {stats['breakeven']} |",
        f"| Win rate | {win_rate:.1f}% |",
        f"| Total P/L | {total_pl:+.2f} USD |",
        f"| Average win | {avg_win:+.2f} USD |",
        f"| Average loss | {avg_loss:+.2f} USD |",
        f"| Starting balance | {start_balance:.2f} USD |",
        f"| Ending balance | {end_balance:.2f} USD |",
        f"| Current equity | {current_equity:.2f} USD |",
        "",
        "## Trades",
        "",
    ]

    if not trades:
        lines.append("_No trades closed on this date._")
    else:
        lines.append("| # | Direction | Entry Time | Exit Time | Entry Price | Exit Price | Lots | P/L (USD) | Exit Reason |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            lines.append(
                f"| {i} | {t['direction']} | {t['entry_time'].strftime('%H:%M:%S')} | "
                f"{t['exit_time'].strftime('%H:%M:%S')} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | "
                f"{t['volume']:.2f} | {t['profit']:+.2f} | {t['exit_reason']} |"
            )

    lines += [
        "",
        "---",
        "_Starting/ending balance are reconstructed from the full account deal "
        "history (all trades + deposits/withdrawals), not just this bot's trades — "
        "so they reflect true account balance even if something else touched this "
        "account today. The trade table and win-rate above are filtered to this "
        "bot's trades only (matched by symbol + magic number)._",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(COLOMBO).date()

    config = load_config(args.account)
    connector = MT5Connector(config.mt5)
    connector.connect()

    try:
        # MT5's own deal .time fields use the broker's own time convention,
        # NOT true UTC -- see mt5_utc_offset's docstring. Measure it fresh
        # via the connector, same pattern used elsewhere in this project.
        offset = mt5_utc_offset(connector, config.symbol)
        trades = get_closed_trades(config.symbol, config.execution.magic_number, target_date, offset)

        account = connector.account_info()
        current_balance = account.balance
        current_equity = account.equity

        day_start_utc, day_end_utc = day_bounds_utc(target_date)
        start_balance = get_balance_at(day_start_utc, current_balance)
        end_balance = get_balance_at(day_end_utc, current_balance)

        report = build_report_markdown(target_date, trades, start_balance, end_balance, current_equity, config.symbol)

        reports_dir = Path(__file__).resolve().parent.parent / "reports" / "daily" / args.account
        reports_dir.mkdir(parents=True, exist_ok=True)
        out_path = reports_dir / f"{target_date.isoformat()}.md"
        out_path.write_text(report)

        total_pl = sum(t["profit"] for t in trades)
        print(f"Report written to {out_path}")
        print(f"Trades: {len(trades)} | Total P/L: {total_pl:+.2f} USD")

    finally:
        connector.disconnect()


if __name__ == "__main__":
    main()
