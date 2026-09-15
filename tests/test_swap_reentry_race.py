"""A swap must re-enter, not go flat.

    python3 tests/test_swap_reentry_race.py

REAL INCIDENT, live2_m3, 2026-09-15 22:54:01, real money:

    .417  trade_exited   SELL cross -> closing the BUY   (close SUCCEEDED)
    .424  entry_blocked_existing_position
          "SELL entry REFUSED: this engine believes it is flat but the
           broker already shows 1 position(s) ... (tickets 95544913)"

Seven milliseconds. MT5's positions_get() reads the terminal's LOCAL cache,
which the server updates asynchronously, so a successfully closed position
is still listed for a moment afterwards. The swap closes and re-enters in
the same breath, so the duplicate-position guard read that stale cache and
refused the reversal. The BUY closed; the SELL never opened. The swap --
this strategy's main exit AND its re-entry -- went flat instead of
reversing.

The guard itself is right and stays: on 2026-09-08 two processes on
demo2_m1 each opened a SELL a second apart, 0.12 lots became 0.24. What it
could not do was tell a stale read from a real duplicate.

TICKETS separate them. A position this engine just closed is one it knows
by ticket; a duplicate opened by anything else carries a ticket it has
never seen. So a just-closed ticket is exempt, briefly, and everything else
is still refused.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, ".")
_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
_stub.ORDER_TYPE_BUY = 0
_stub.ORDER_TYPE_SELL = 1
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

ROOT = Path(__file__).resolve().parents[1]
ENGINES = [
    ROOT / "bot" / "strategy" / "state_machine_dual_cross_confirmed_swap.py",
    ROOT / "bot" / "strategy" / "state_machine_dual_cross_confirmed_swap_adx.py",
]

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Pos:
    def __init__(self, ticket): self.ticket = ticket


WINDOW = 30.0


def decide(open_positions, recently_closed, now):
    """The guard's logic, exactly as the engines now run it."""
    recently_closed = {t: ts for t, ts in recently_closed.items() if now - ts < WINDOW}
    stale = [p for p in open_positions if p.ticket in recently_closed]
    genuine = [p for p in open_positions if p.ticket not in recently_closed]
    return ("REFUSED" if genuine else "ALLOWED"), stale, genuine


def main() -> None:
    now = time.monotonic()

    print("the live incident: swap re-entry racing its own close")
    verdict, stale, genuine = decide([Pos(95544913)], {95544913: now - 0.007}, now)
    check("a ticket closed 7ms ago no longer blocks the re-entry", verdict == "ALLOWED")
    check("and it is reported as stale, not silently ignored",
          [p.ticket for p in stale] == [95544913])

    print("\nthe 2026-09-08 duplicate is still refused")
    verdict, _, genuine = decide([Pos(777777)], {}, now)
    check("a ticket this engine never closed still blocks", verdict == "REFUSED")
    check("and it is named", [p.ticket for p in genuine] == [777777])

    print("\nmixed: our stale one AND a real duplicate")
    verdict, stale, genuine = decide([Pos(95544913), Pos(777777)],
                                     {95544913: now - 0.007}, now)
    check("still refuses — the foreign position decides it", verdict == "REFUSED")
    check("only the foreign ticket is counted against us",
          [p.ticket for p in genuine] == [777777])

    print("\nthe exemption expires")
    verdict, _, _ = decide([Pos(95544913)], {95544913: now - (WINDOW + 1)}, now)
    check("a ticket 'closed' 31s ago and STILL open is a real problem again",
          verdict == "REFUSED")

    print("\nflat broker, nothing to decide")
    verdict, _, _ = decide([], {95544913: now - 0.007}, now)
    check("no open positions -> entry allowed", verdict == "ALLOWED")

    print("\nboth deployed engines carry the fix")
    for path in ENGINES:
        src = path.read_text(encoding="utf-8")
        check(f"{path.name}: records the closed ticket",
              "self.recently_closed[position.ticket] = time.monotonic()" in src)
        check(f"{path.name}: exempts it in the guard",
              "p.ticket not in self.recently_closed" in src)
        check(f"{path.name}: records BEFORE clearing state",
              src.index("self.recently_closed[position.ticket]")
              < src.index("self.position = None\n        log_decision"))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
