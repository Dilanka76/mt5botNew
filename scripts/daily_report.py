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
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MetaTrader5 as mt5

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")
LOOKBACK_DAYS = 5  # how far before the report date to search for a trade's ENTRY deal, in case it opened earlier


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


def classify_exit_reason(exit_deal) -> str:
    """Our own closes (opposite-EMA-cross exits, see trade_executor.close_position)
    tag the comment with a "-close" suffix — check that first since it's an explicit
    signal. Otherwise fall back to MT5's own deal.reason code for a broker-driven fill."""
    comment = exit_deal.comment or ""
    if comment.lower().endswith("-close"):
        return "EMA Cross Exit"
    if exit_deal.reason == mt5.DEAL_REASON_TP:
        return "Take Profit"
    if exit_deal.reason == mt5.DEAL_REASON_SL:
        return "Stop Loss"
    return f"Unknown (reason={exit_deal.reason}, comment={comment!r})"


def day_bounds_utc(target_date: date_cls) -> tuple[datetime, datetime]:
    day_start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=COLOMBO)
    day_end_local = day_start_local + timedelta(days=1)
    return day_start_local.astimezone(timezone.utc), day_end_local.astimezone(timezone.utc)


def get_closed_trades(symbol: str, magic: int, target_date: date_cls) -> list[dict]:
    """Pairs entry/exit deals (by position_id) for THIS bot's trades
    (filtered by symbol + magic number) whose EXIT fell on target_date."""
    day_start_utc, day_end_utc = day_bounds_utc(target_date)
    query_from = day_start_utc - timedelta(days=LOOKBACK_DAYS)

    deals = mt5.history_deals_get(query_from, day_end_utc)
    if not deals:
        return []

    relevant = [d for d in deals if d.symbol == symbol and d.magic == magic]

    by_position: dict[int, list] = {}
    for d in relevant:
        by_position.setdefault(d.position_id, []).append(d)

    trades = []
    for position_id, deal_list in by_position.items():
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exit_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_OUT), None)
        if entry_deal is None or exit_deal is None:
            continue  # still open, or entry fell outside our lookback window

        exit_time_utc = datetime.fromtimestamp(exit_deal.time, tz=timezone.utc)
        if not (day_start_utc <= exit_time_utc < day_end_utc):
            continue  # closed on a different day

        direction = "BUY" if entry_deal.type == mt5.ORDER_TYPE_BUY else "SELL"
        profit = exit_deal.profit + exit_deal.swap + exit_deal.commission + entry_deal.commission

        trades.append({
            "position_id": position_id,
            "direction": direction,
            "volume": exit_deal.volume,
            "entry_time": datetime.fromtimestamp(entry_deal.time, tz=timezone.utc).astimezone(COLOMBO),
            "exit_time": exit_time_utc.astimezone(COLOMBO),
            "entry_price": entry_deal.price,
            "exit_price": exit_deal.price,
            "profit": profit,
            "exit_reason": classify_exit_reason(exit_deal),
        })

    trades.sort(key=lambda t: t["entry_time"])
    return trades


def get_balance_at(target_utc_moment: datetime, current_balance: float) -> float:
    """Reconstructs the account balance at a past UTC moment by reversing
    out every balance-affecting deal (ANY trade or deposit/withdrawal on
    the whole account, not just this bot's) that happened between then and
    now. If target is in the future (e.g. "today" hasn't ended), returns
    the current balance."""
    now = datetime.now(timezone.utc)
    if target_utc_moment >= now:
        return current_balance
    deals = mt5.history_deals_get(target_utc_moment, now)
    if not deals:
        return current_balance
    total_change_since = sum((d.profit or 0) + (d.swap or 0) + (d.commission or 0) for d in deals)
    return current_balance - total_change_since


def build_report_markdown(
    target_date: date_cls, trades: list[dict], start_balance: float, end_balance: float,
    current_equity: float, symbol: str,
) -> str:
    total = len(trades)
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] < 0]
    breakeven = total - len(wins) - len(losses)
    win_rate = (len(wins) / total * 100) if total else 0.0
    total_pl = sum(t["profit"] for t in trades)
    avg_win = (sum(t["profit"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["profit"] for t in losses) / len(losses)) if losses else 0.0

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
        f"| Wins / Losses / Breakeven | {len(wins)} / {len(losses)} / {breakeven} |",
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
        trades = get_closed_trades(config.symbol, config.execution.magic_number, target_date)

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
