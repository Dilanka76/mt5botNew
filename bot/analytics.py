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


# Plausible range for this broker's server-time offset. It has been UTC+3
# throughout this project; a DST change shifts a server clock by exactly
# one hour. Anything outside this means the measurement is bad (almost
# always a stale tick from a closed market) -- or the broker genuinely
# moved, which is worth a human's attention rather than silent acceptance.
PLAUSIBLE_OFFSET_MIN = timedelta(hours=1)
PLAUSIBLE_OFFSET_MAX = timedelta(hours=4)


class StaleTickError(RuntimeError):
    """The newest MT5 tick is too old to measure the broker time offset
    from -- almost always because the market is closed. Raised instead of
    returning a wrong offset, which would silently corrupt every reported
    trade time. See mt5_utc_offset()."""


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
    time convention, not true UTC).

    STALE-TICK GUARD (added 2026-09-05 after a real incident). This
    measurement only works while ticks are actually flowing: it assumes
    the newest tick IS "now" in broker time. With the market closed the
    newest tick is Friday's last one, so the subtraction returns the
    AGE OF THAT STALE TICK instead of the broker's timezone offset. Run
    on a Saturday it returned -7.66h instead of the true +3h, silently
    shifting every reported trade time by 10.66 hours -- which is how it
    was found, from trades appearing in a loss report at times that did
    not match decisions.jsonl.

    A broker's server time is a timezone, so a genuine offset is always
    a multiple of 15 minutes; a live measurement lands within a second
    or two of one. Anything further off means the tick is stale, and we
    raise rather than return a wrong number -- silently wrong report
    times cost more than a failed report. The returned value is snapped
    to the exact quarter-hour, removing the sub-second measurement noise
    the old version carried through.

    Residual limitation, deliberately accepted: if the market happened
    to close an exact multiple of 15 minutes ago, the staleness check
    cannot tell. Nothing cheap distinguishes that case, and it is far
    narrower than the bug it replaces."""
    tick = connector.get_tick(symbol)
    mt5_now = datetime.fromtimestamp(tick.time, tz=timezone.utc)
    true_now = datetime.now(timezone.utc)
    raw = mt5_now - true_now

    quarter = timedelta(minutes=15)
    nearest = round(raw / quarter) * quarter
    drift = abs(raw - nearest)

    # PLAUSIBILITY CHECK -- the quarter-hour test alone is NOT enough. A
    # stale tick's apparent offset sweeps continuously as the clock
    # advances, so every 15 minutes it passes within seconds of a
    # quarter-hour value and looks valid. That hole was hit within
    # minutes of shipping the first version of this guard: a Saturday
    # run returned -7:45:00, which is not even a real timezone, and
    # produced a report whose times were all wrong by a clean-looking
    # amount. Shape alone proves nothing; the value must also be
    # possible. This broker has been UTC+3 for the life of this project
    # and a DST shift moves a server clock by exactly one hour, so
    # anything outside this window means a stale tick -- or a genuine
    # broker change, which should stop a human rather than be absorbed
    # silently.
    if not (PLAUSIBLE_OFFSET_MIN <= nearest <= PLAUSIBLE_OFFSET_MAX):
        raise StaleTickError(
            f"Implausible MT5 time offset for {symbol}: {nearest} "
            f"({nearest.total_seconds()/3600:+.2f}h).\n"
            f"  newest tick (read as UTC): {mt5_now.isoformat()}\n"
            f"  true UTC now             : {true_now.isoformat()}\n"
            f"  raw difference           : {raw} ({raw.total_seconds()/3600:+.2f}h)\n"
            f"Expected between {PLAUSIBLE_OFFSET_MIN} and {PLAUSIBLE_OFFSET_MAX} "
            f"(this broker runs UTC+3; DST moves a server clock by one hour at most).\n"
            f"This almost always means the market is CLOSED and the newest tick is left over "
            f"from the last session -- its apparent 'offset' is really the tick's age. Re-run "
            f"when the market is open.\n"
            f"If the broker has genuinely changed its server timezone, update "
            f"PLAUSIBLE_OFFSET_MIN/MAX in bot/analytics.py deliberately, after confirming it."
        )

    if drift > timedelta(seconds=120):
        raise StaleTickError(
            f"Cannot measure the MT5 time offset for {symbol}: the newest tick is stale.\n"
            f"  newest tick (read as UTC): {mt5_now.isoformat()}\n"
            f"  true UTC now             : {true_now.isoformat()}\n"
            f"  raw difference           : {raw} "
            f"({raw.total_seconds()/3600:+.2f}h)\n"
            f"  nearest valid offset     : {nearest} -- off by {drift}, far more than a live "
            f"tick's 1-2 seconds.\n"
            f"A real broker offset is always a whole quarter-hour. This almost always means the "
            f"market is CLOSED (weekend or holiday) and the newest tick is left over from the "
            f"last session. Re-run when the market is open. Refusing to return a wrong offset -- "
            f"it would silently shift every reported trade time."
        )
    return nearest


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


def _our_closed_positions(deals, symbol: str, magic: int, offset: timedelta) -> list[dict]:
    """Every FULLY closed position this bot opened, however it was closed.

    A position is ours when its ENTRY deal carries our magic number -- but
    its exit may not. FIXED 2026-09-22: both callers used to keep only
    deals with our magic, and a close from the MT5 phone app, the desktop
    terminal or the web carries magic 0. The exit was dropped, the pairing
    found no exit, and the whole trade silently vanished -- live2 had 12
    hand-closed trades the broker could see and every report built on
    this function could not. It also cut partial closes short: a 0.03-lot
    trade closed in parts showed as 0.01 lots and a third of its profit.

    Now: ours = positions whose entry has our magic; then EVERY deal of
    those positions counts, whatever its magic. Volume is the entry's,
    the exit price is the volume-weighted average of the exits, profit
    sums every leg, and a position still partly open is left out.
    """
    ours = {d.position_id for d in deals
            if d.symbol == symbol and d.magic == magic and d.entry == mt5.DEAL_ENTRY_IN}
    by_position: dict[int, list] = {}
    for d in deals:
        if d.symbol == symbol and d.position_id in ours:
            by_position.setdefault(d.position_id, []).append(d)

    out_entries = (mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", 3))
    trades = []
    for position_id, deal_list in by_position.items():
        entry_deal = next((d for d in deal_list if d.entry == mt5.DEAL_ENTRY_IN), None)
        exits = sorted((d for d in deal_list if d.entry in out_entries), key=lambda d: d.time)
        if entry_deal is None or not exits:
            continue  # still open, or the entry fell outside the queried range
        closed_volume = sum(d.volume for d in exits)
        if closed_volume + 1e-9 < entry_deal.volume:
            continue  # partly closed and still open
        last = exits[-1]
        exit_time_utc = datetime.fromtimestamp(last.time, tz=timezone.utc) - offset
        trades.append({
            "position_id": position_id,
            "ticket": position_id,
            "direction": "BUY" if entry_deal.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume": entry_deal.volume,
            "entry_time": (datetime.fromtimestamp(entry_deal.time, tz=timezone.utc) - offset).astimezone(COLOMBO),
            "exit_time": exit_time_utc.astimezone(COLOMBO),
            "exit_time_utc": exit_time_utc,
            "entry_price": entry_deal.price,
            "exit_price": sum(d.price * d.volume for d in exits) / closed_volume,
            "profit": entry_deal.commission + sum(d.profit + d.swap + d.commission for d in exits),
            "exit_reason": classify_exit_reason(last),
            "exit_deals": len(exits),
        })
    trades.sort(key=lambda t: t["entry_time"])
    return trades


def get_closed_trades(symbol: str, magic: int, target_date: date_cls, offset: timedelta) -> list[dict]:
    """Pairs entry/exit deals (by position_id) for THIS bot's trades
    (filtered by symbol + magic number) whose EXIT fell on target_date.

    `offset` is the broker-vs-true-UTC offset (see mt5_utc_offset's
    docstring) — MUST be measured by the caller via mt5_utc_offset(connector,
    symbol) and passed in; this function has no connector of its own to
    measure it fresh. Applied to the query boundaries (ADDED, since
    mt5.history_deals_get expects broker time) and to each returned deal's
    .time field (SUBTRACTED, to get true UTC before the day-boundary check
    and the Colombo conversion) — confirmed 2026-08-27 this was missing
    entirely, producing entry/exit times off by the broker's offset (+3h)
    on every trade (see get_closed_trades_range's identical fix, same
    day) — P/L amounts (computed from prices, not timestamps) were
    unaffected, but a trade closing near a day boundary could have been
    silently placed in the wrong day's report."""
    day_start_utc, day_end_utc = day_bounds_utc(target_date)
    query_from = day_start_utc - timedelta(days=LOOKBACK_DAYS)

    deals = mt5.history_deals_get(query_from + offset, day_end_utc + offset)
    if not deals:
        return []

    trades = []
    for t in _our_closed_positions(deals, symbol, magic, offset):
        if not (day_start_utc <= t["exit_time_utc"] < day_end_utc):
            continue  # closed on a different day
        trades.append(t)
    return trades


def get_closed_trades_range(
    symbol: str, magic: int, date_from_utc: datetime, date_to_utc: datetime, offset: timedelta,
) -> list[dict]:
    """Same entry/exit deal pairing and dict shape as get_closed_trades()
    above, but over an explicit UTC range instead of a single calendar day
    + fixed lookback — for reports that need this bot's FULL trade history
    in one query (e.g. scripts/generate_live_test_report.py) rather than
    looping get_closed_trades() one day at a time. "ticket" is the
    position ticket (== position_id, matching bot.logging_setup.logger's
    trade_exited/trade_closed_tp "ticket" field), not a deal ticket — so
    callers can join against logs/<account>/decisions.jsonl.

    `offset` is the broker-vs-true-UTC offset (see mt5_utc_offset's
    docstring) — MUST be measured by the caller via mt5_utc_offset(connector,
    symbol) and passed in; this function has no connector of its own to
    measure it fresh. Applied both to the query boundaries (ADDED, since
    mt5.history_deals_get expects broker time) and to each returned deal's
    .time field (SUBTRACTED, to get true UTC before the Colombo
    conversion) — confirmed 2026-08-27 this was missing entirely in an
    earlier version of this function, producing entry/exit times off by
    the broker's offset (+3h) on every trade in
    scripts/generate_live_test_report.py's Detail tabs, even though the
    P/L amounts (computed from prices, not timestamps) were unaffected."""
    deals = mt5.history_deals_get(date_from_utc + offset, date_to_utc + offset)
    if not deals:
        return []

    return _our_closed_positions(deals, symbol, magic, offset)


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
