"""The broker backstop must go in with the order, and never be the real stop.

    python3 tests/test_broker_backstop.py

User's decision 2026-09-13 (option B, at $30/$35): do not put the
STRATEGY's stop at the broker -- a visible stop at the level everyone
would guess is what stop-hunting targets -- but do place a much wider one
as a disaster brake.

    live2_m3   software stop $7     broker backstop $30
    live2_m5   software stop $10    broker backstop $35

Two properties matter.

ATOMIC. The stop must be in the SAME order request, not a follow-up
set_sltp. Between an order filling and a separate modify there is a
window with no stop at all, and surviving the bot dying is the entire
point of the thing. It cannot die in a window that does not exist.

MUCH WIDER. If the backstop were ever set at or near the software stop it
would start firing on ordinary losing trades, changing the strategy
rather than insuring it -- and it would sit exactly where a hunt would
look, which is what the user asked to avoid.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXEC = (ROOT / "bot" / "execution" / "trade_executor.py").read_text(encoding="utf-8")
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> None:
    print("broker backstop")

    check("open_market_order accepts a stop distance",
          "stop_loss_distance: float | None = None" in EXEC)
    check("the stop is put in the ORDER request, not a later modify",
          'request["sl"] = stop_loss' in EXEC)
    # It must be set before order_send, or it is not atomic.
    check("and before order_send, so there is no unprotected window",
          EXEC.index('request["sl"] = stop_loss') < EXEC.index("result = mt5.order_send(request)"))
    check("no distance means no stop key at all (unchanged behaviour)",
          "if stop_loss is not None:" in EXEC)

    print("\n  direction")
    # A BUY's stop sits BELOW entry, a SELL's ABOVE. Reversed, it would
    # close instantly at a loss on every trade.
    check("BUY subtracts, SELL adds",
          "price - stop_loss_distance if direction == Direction.BUY" in EXEC
          and "else price + stop_loss_distance" in EXEC)

    print("\n  both engines pass it through")
    for name in ("state_machine_dual_cross_confirmed_swap_adx.py",
                 "state_machine_dual_cross_confirmed_swap.py"):
        src = (ROOT / "bot" / "strategy" / name).read_text(encoding="utf-8")
        short = name.replace("state_machine_dual_cross_confirmed_swap", "").replace(".py", "") or "_plain"
        check(f"{short}: passes config.broker_backstop_usd to the order",
              "self.config.broker_backstop_usd)" in src)

    print("\n  the arithmetic the user chose")
    for leg, soft, back, lots in (("live2_m3", 7.0, 30.0, 0.04), ("live2_m5", 10.0, 35.0, 0.02)):
        check(f"{leg}: backstop ${back:.0f} is far wider than the ${soft:.0f} software stop",
              back >= soft * 3)
        worst = back * lots * 100
        print(f"         worst case if the bot dies: ${worst:.0f} "
              f"(vs ${soft * lots * 100:.0f} intended, vs unlimited before)")

    print("\n  config default is OFF")
    cfg = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
    check("broker_backstop_usd defaults to None",
          "broker_backstop_usd: float | None = None" in cfg)
    check("and is read from the config file",
          'broker_backstop_usd=raw.get("broker_backstop_usd")' in cfg)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
