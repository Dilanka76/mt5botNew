"""Daily loss limit: stop opening trades once today's realised losses
reach a set amount.

Agreed with the user three times (2026-09-01, 2026-09-05, 2026-09-07) and
built on the third, ahead of live2 going live with real money at ~8.7%
risk per trade across TWO legs -- so both legs holding a position at once
puts roughly 17% of the account at risk, and a six-loss run (which the
streak measurement showed really happens) is a very bad day with no cap
under it.

Deliberately pure and MT5-free, like bot/trade_ledger.py and
bot/status_writer.py: it reads the append-only local ledger
(logs/<account>/trade_history.jsonl) that main.py already maintains. That
keeps it instantly testable, adds no broker round-trip to the hot path,
and means a broker hiccup cannot make the limit misbehave.

WHAT IT DOES AND DOES NOT DO:
  - Blocks NEW entries for the rest of the Colombo calendar day.
  - Never touches an OPEN position. A trade already running keeps its
    stop, its take-profit and its swap exit. Force-closing on a threshold
    would turn a floating loss into a realised one at an arbitrary
    moment, which is the opposite of protection.
  - Resets at Colombo midnight, the same day boundary the dashboard and
    bot/trade_stats.py already use.
"""
from __future__ import annotations

import json
from datetime import date as date_cls, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import PROJECT_ROOT

COLOMBO = ZoneInfo("Asia/Colombo")


def _ledger_path(log_dir: str, account: str) -> Path:
    # Same location bot/trade_ledger.py writes to.
    return PROJECT_ROOT / log_dir / account / "trade_history.jsonl"


def realized_pl_today(log_dir: str, account: str, today: date_cls | None = None) -> float:
    """Sum of this account's realised P/L for the current Colombo day.

    Missing ledger -> 0.0 (no trades recorded yet, not an error).
    Malformed or incomplete lines are skipped rather than failing the
    read: a half-written line from a crash mid-append must not be able to
    disable the limit or take down the trading loop.
    """
    path = _ledger_path(log_dir, account)
    if not path.exists():
        return 0.0

    today = today or datetime.now(COLOMBO).date()
    total = 0.0
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trade = json.loads(line)
            closed = datetime.fromisoformat(trade["close_time"]).astimezone(COLOMBO).date()
            profit = float(trade["profit"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        if closed == today:
            total += profit
    return total


def daily_limit_reason(log_dir: str, account: str, limit_usd: float | None,
                       today: date_cls | None = None) -> str | None:
    """None if trading may continue; otherwise a human-readable reason.

    `limit_usd` is read as a magnitude, so both 50 and -50 mean "stop
    after $50 of losses" -- a sign slip in a config file must not silently
    disable the one rule whose whole job is to stop losses.
    """
    if limit_usd is None:
        return None
    threshold = abs(float(limit_usd))
    if threshold == 0:
        return None
    realized = realized_pl_today(log_dir, account, today)
    if realized <= -threshold:
        return (f"daily loss limit reached: ${realized:.2f} realised today "
                f"(limit ${threshold:.2f}) — no new entries until Colombo midnight")
    return None
