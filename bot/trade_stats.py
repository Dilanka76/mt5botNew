"""Pure trade-statistics aggregation — win rate, P/L, daily/hourly
breakdowns — operating only on plain dicts (never raw MT5 objects).

Deliberately MT5-free, like bot/status_writer.py: api_server.py imports
this module directly to compute the /apiconnect/{account}/analytics
response live from the local trade ledger (bot/trade_ledger.py), and must
stay importable on a machine with no MetaTrader5 package at all.

Every function here accepts trade dicts with (at minimum) a numeric
"profit" key and an ISO "close_time" key — both `get_closed_trades()`
(bot/analytics.py, richer shape, used by daily_report.py) and the trade
ledger (bot/trade_ledger.py, simpler shape, used here) satisfy that.
"""
from __future__ import annotations

import json
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

COLOMBO = ZoneInfo("Asia/Colombo")


def compute_day_stats(trades: list[dict]) -> dict:
    """Win/loss/breakeven counts, win rate, total/average P/L for one
    bucket of trades — the same math daily_report.py always did, just
    factored out so both it and the ledger-based dashboard endpoint share
    one implementation."""
    total = len(trades)
    wins = [t for t in trades if t["profit"] > 0]
    losses = [t for t in trades if t["profit"] < 0]
    breakeven = total - len(wins) - len(losses)
    win_rate = (len(wins) / total * 100) if total else 0.0
    total_pl = sum(t["profit"] for t in trades)
    avg_win = (sum(t["profit"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["profit"] for t in losses) / len(losses)) if losses else 0.0

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": win_rate,
        "total_pl": total_pl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def read_trade_ledger(path: Path) -> list[dict]:
    """Reads the append-only JSONL trade ledger. Missing file -> empty
    list (no trades recorded yet, not an error). Malformed lines are
    skipped defensively rather than failing the whole read — a single
    corrupted line (e.g. a half-written line from a crash mid-append)
    shouldn't take down the entire analytics view."""
    if not path.exists():
        return []
    trades = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return trades


def _close_date_colombo(trade: dict) -> date_cls:
    return datetime.fromisoformat(trade["close_time"]).astimezone(COLOMBO).date()


def _close_hour_colombo(trade: dict) -> int:
    return datetime.fromisoformat(trade["close_time"]).astimezone(COLOMBO).hour


def compute_daily_breakdown(trades: list[dict], days: int = 30, today: date_cls | None = None) -> list[dict]:
    """One compute_day_stats() bucket per calendar day (Asia/Colombo),
    oldest first, for the last `days` days including today. Days with no
    trades still appear, with all-zero stats — callers (chart data) don't
    have to fill gaps themselves."""
    today = today or datetime.now(COLOMBO).date()
    buckets: dict[date_cls, list[dict]] = {}
    for t in trades:
        try:
            d = _close_date_colombo(t)
        except (KeyError, ValueError):
            continue
        buckets.setdefault(d, []).append(t)

    result = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        stats = compute_day_stats(buckets.get(d, []))
        result.append({"date": d.isoformat(), **stats})
    return result


def compute_hourly_breakdown(trades: list[dict], target_date: date_cls) -> list[dict]:
    """One compute_day_stats() bucket per hour (0-23, Asia/Colombo) for a
    single calendar day — used for "today so far" detail."""
    day_trades = [t for t in trades if _matches_date(t, target_date)]
    buckets: dict[int, list[dict]] = {}
    for t in day_trades:
        buckets.setdefault(_close_hour_colombo(t), []).append(t)

    result = []
    for hour in range(24):
        stats = compute_day_stats(buckets.get(hour, []))
        result.append({"hour": hour, **stats})
    return result


def _matches_date(trade: dict, target_date: date_cls) -> bool:
    try:
        return _close_date_colombo(trade) == target_date
    except (KeyError, ValueError):
        return False
