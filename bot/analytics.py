"""MT5-touching trade-history queries, shared by scripts/daily_report.py
and main.py's trade-ledger writer (bot/trade_ledger.py's ledger ENTRIES are
built by main.py using trade_profit() below, since main.py already has the
raw MT5 deal objects at that point).

Deliberately separate from bot/trade_stats.py: this module imports
MetaTrader5 (it needs raw MT5 deal objects), so nothing that must stay
MT5-free (api_server.py, bot/status_writer.py, bot/trade_ledger.py,
bot/trade_stats.py) may import from here.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

COLOMBO = ZoneInfo("Asia/Colombo")
LOOKBACK_DAYS = 5  # how far before a report date to search for a trade's ENTRY deal, in case it opened earlier


def mt5_utc_offset(connector, symbol: str) -> timedelta:
    """Measures, RIGHT NOW, how far MT5's own reported time is from true
    UTC — confirmed 2026-08-19 to be a real, exact offset (MT5 tick.time
    read as 21:24:14 while true UTC and the server's own OS clock both
    agreed on 18:24:14 — a clean +3h gap, not machine clock drift).
    Broker "server time" conventions like this commonly follow a DST
    schedule, so this is measured fresh each call rather than hardcoded —
    a stale hardcoded constant would silently go wrong the next time the
    broker's DST rule flips. Apply the returned offset by SUBTRACTING it
    from any deal/position .time field (already read via
    datetime.fromtimestamp(x, tz=timezone.utc)) to get true UTC, or by
    ADDING it to a true-UTC query boundary before passing to
    mt5.history_deals_get()/history_orders_get() (which expect MT5's own
    time convention, not true UTC)."""
    tick = connector.get_tick(symbol)
    mt5_now = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    true_now = datetime.now(timezone.utc)
    return mt5_now - true_now


def trade_profit(exit_deal, entry_deal) -> float:
    """A trade's true realized profit: the exit deal's profit plus BOTH
    legs' swap/commission (MT5 attributes these separately per deal, and
    commission in particular is commonly charged on the entry leg, not
    just the exit)."""
    return exit_deal.profit + exit_deal.swap + exit_deal.commission + entry_deal.commission


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

        trades.append({
            "position_id": position_id,
            "direction": direction,
            "volume": exit_deal.volume,
            "entry_time": datetime.fromtimestamp(entry_deal.time, tz=timezone.utc).astimezone(COLOMBO),
            "exit_time": exit_time_utc.astimezone(COLOMBO),
            "entry_price": entry_deal.price,
            "exit_price": exit_deal.price,
            "profit": trade_profit(exit_deal, entry_deal),
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
