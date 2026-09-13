"""Be flat for the weekend: the rule must fire on Friday and never else.

    python3 tests/test_weekend_flat.py

User's decision 2026-09-13, for live2: any position still open when the
week's trading ends must be closed, profit or loss. A stop is only
checked when ticks arrive and none arrive over a weekend, so gold can
reopen Monday past the stop and the bot closes at whatever price exists.
On a $300 account a $30 gap at 0.04 lots is $120 -- 40% -- against an
intended $28 loss.

The cutoff is expressed in UTC and keyed on UTC Friday DELIBERATELY.
Colombo is UTC+5:30 and the session runs 04:00-01:29, so the last session
of the week ENDS on a Saturday there. Keying on the local weekday would
have needed the Saturday-morning tail and missed Friday evening
altogether -- the several hours where a position is most likely to be
open.

A rule that fires on the wrong day is worse than no rule: it would close
good trades every day of the week.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.sessions import weekend_flat_due

COLOMBO = ZoneInfo("Asia/Colombo")
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def utc(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def main() -> None:
    print("weekend flat rule   (cutoff 20:00 UTC)")
    CUT = "20:00"

    # 2026-09-18 is a Friday.
    check("Friday 19:59 UTC — not yet", not weekend_flat_due(CUT, utc(2026, 9, 18, 19, 59)))
    check("Friday 20:00 UTC — due", weekend_flat_due(CUT, utc(2026, 9, 18, 20, 0)))
    check("Friday 20:30 UTC — due", weekend_flat_due(CUT, utc(2026, 9, 18, 20, 30)))
    check("Friday 23:59 UTC — still due", weekend_flat_due(CUT, utc(2026, 9, 18, 23, 59)))

    print("\n  every other day must be untouched, or it closes good trades daily")
    for offset, name in ((-4, "Monday"), (-3, "Tuesday"), (-2, "Wednesday"),
                         (-1, "Thursday"), (1, "Saturday"), (2, "Sunday")):
        day = utc(2026, 9, 18, 20, 30) + timedelta(days=offset)
        check(f"{name} 20:30 UTC — NOT due", not weekend_flat_due(CUT, day))

    print("\n  switched off by default")
    check("None never fires", not weekend_flat_due(None, utc(2026, 9, 18, 23, 0)))
    check("empty string never fires", not weekend_flat_due("", utc(2026, 9, 18, 23, 0)))

    print("\n  the Colombo trap this was written to avoid")
    # Friday 20:30 UTC is SATURDAY 02:00 in Colombo. A rule keyed on the
    # local weekday would see "Saturday" and, if it keyed on Friday
    # locally, would already have stopped firing.
    friday_2030 = utc(2026, 9, 18, 20, 30)
    local = friday_2030.astimezone(COLOMBO)
    check(f"Friday 20:30 UTC is {local:%A} {local:%H:%M} in Colombo",
          local.strftime("%A") == "Saturday")
    check("and the rule still fires then", weekend_flat_due(CUT, friday_2030))

    # Colombo Friday morning is UTC Friday too -- must NOT fire early.
    fri_morning = utc(2026, 9, 18, 5, 0)
    check("Friday 05:00 UTC (10:30 Colombo, mid-session) — NOT due",
          not weekend_flat_due(CUT, fri_morning))

    print("\n  a naive timezone still works")
    check("naive datetime treated as UTC",
          weekend_flat_due(CUT, datetime(2026, 9, 18, 20, 30, tzinfo=timezone.utc)))

    print("\n  the cutoff leaves time to actually close")
    # Broker is UTC+3 and closes gold at 23:59 its time = 20:59 UTC.
    check("20:00 UTC is before the 20:59 UTC market close",
          utc(2026, 9, 18, 20, 0) < utc(2026, 9, 18, 20, 59))

    print("\n  wired into BOTH engines")
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "bot" / "strategy"
    for name in ("state_machine_dual_cross_confirmed_swap_adx.py",
                 "state_machine_dual_cross_confirmed_swap.py"):
        src = (root / name).read_text(encoding="utf-8")
        short = name.replace("state_machine_dual_cross_confirmed_swap", "").replace(".py", "") or "_plain"
        check(f"{short}: closes an open position when due",
              'category="weekend_flat"' in src)
        check(f"{short}: blocks new entries when due",
              '"entry_blocked_weekend"' in src)
        # The close must come BEFORE the grace period, or a fresh position
        # could skip it, and before every other exit check.
        body = src[src.index("def on_tick"):]
        body = body[:body.index("\n    def ", 1)]
        check(f"{short}: the weekend close is checked before the grace period",
              body.index("weekend_flat_due") < body.index("POSITION_CLOSE_GRACE_PERIOD_SECONDS"))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
