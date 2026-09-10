"""Formatting a setting that is allowed to be absent.

Several config fields are legitimately None -- stop_loss_usd (demo2_m3
holds losers to the opposite cross), breakeven_trigger_usd,
tp_runner_trail_usd, htf_trend_take_profit_usd, daily_loss_limit_usd.
Every one of them was once a required number, and reporting code written
back then formats them directly:

    f"stop ${c.stop_loss_usd:.2f}"

which raises TypeError: unsupported format string passed to
NoneType.__format__ the moment the setting is switched off. That happened
four times on 2026-09-09/10 -- in deploy_report's header, bot/config.py's
reader, set_lot_ladder's tier line and audit_recent_trades' header --
each time only when the tool was pointed at the one account using the
feature, so each looked like an isolated bug rather than one pattern.
"""
from __future__ import annotations


def usd(value: float | None, absent: str = "none") -> str:
    """A dollar amount, or a word when the setting is switched off."""
    return absent if value is None else f"${float(value):.2f}"


def plus_usd(value: float | None, absent: str = "none") -> str:
    """Same, signed -- for a P/L or a difference."""
    return absent if value is None else f"${float(value):+.2f}"
