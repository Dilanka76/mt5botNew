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


def weekend_flat_due(cutoff_utc: str | None, now_utc: datetime | None = None) -> bool:
    """True once it is Friday past `cutoff_utc` — time to be flat for the weekend.

    A position left open when the week's trading ends carries a risk no
    stop can cover: the stop is only checked when ticks arrive, and none
    arrive over a weekend. Gold can reopen Monday well past the stop, and
    the bot then closes at whatever price exists, not at the stop. On a
    $300 account a $30 gap at 0.04 lots is $120 -- 40% -- against an
    intended $28 loss.

    Expressed in UTC and keyed on Friday deliberately. Colombo is UTC+5:30,
    so the last session of the week ENDS on a Saturday there (04:00-01:29
    wraps past midnight) -- keying on the local weekday would need the
    Saturday-morning tail and miss Friday evening entirely. UTC Friday is
    unambiguous.

    The broker (UTC+3) closes gold at 23:59 its time, which is 20:59 UTC,
    so a cutoff of 20:00 UTC leaves an hour of live market to close in.
    """
    if not cutoff_utc:
        return False
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    if now_utc.weekday() != 4:          # 0=Mon .. 4=Fri
        return False
    return now_utc.time() >= _parse_hhmm(cutoff_utc)
