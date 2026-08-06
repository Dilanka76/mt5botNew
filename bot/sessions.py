"""Session-window gating for new trade entries.

Sessions are defined in Sri Lanka wall-clock time (Asia/Colombo, fixed
UTC+5:30, no DST). We compute "now" from UTC rather than the server's local
clock so this is correct regardless of the EC2 instance's OS timezone
setting.
"""
from __future__ import annotations

from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from bot.config import SessionWindow

COLOMBO = ZoneInfo("Asia/Colombo")


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = value.split(":")
    return dt_time(int(hour), int(minute))


def is_within_session(sessions: list[SessionWindow], now_utc: datetime | None = None) -> bool:
    """True if the current Colombo wall-clock time falls inside any configured session."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    current_time = now_utc.astimezone(COLOMBO).time()

    for window in sessions:
        start = _parse_hhmm(window.start)
        end = _parse_hhmm(window.end)
        if start <= end:
            # normal same-day window, e.g. 04:00-08:00
            if start <= current_time <= end:
                return True
        else:
            # overnight window that wraps past midnight, e.g. 12:00-02:30
            # (active from start through midnight, then from midnight through end)
            if current_time >= start or current_time <= end:
                return True

    return False
