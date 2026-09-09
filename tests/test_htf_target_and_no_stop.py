"""demo2_m3's two changes: no stop loss, and a trend-sized take-profit.

    python3 tests/test_htf_target_and_no_stop.py     (needs pandas)

User, 2026-09-09: demo2_m3 holds a losing trade until the opposite cross
(no stop at all), and takes $8 instead of $6 when the trade runs WITH the
M15 EMA13/21 trend.

Removing the stop meant deleting a guard that had been put there on
purpose, so the checks below make sure nothing else still assumes a stop
exists -- a position with stop_loss None must simply never be stopped
out, not crash on the comparison, and not format None into a message.

The parity checks matter just as much: an engine that reads a column the
live loop computes and the backtest does not is the 2026-08-21 fault that
silently disabled a stop-loss for days.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

sys.path.insert(0, ".")
_stub = types.ModuleType("MetaTrader5")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

from bot.strategy.state_machine_dual_cross_confirmed_swap import DualCrossConfirmedSwapEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = (ROOT / "bot" / "strategy" / "state_machine_dual_cross_confirmed_swap.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


class Cfg:
    take_profit_usd = 6.0
    htf_trend_take_profit_usd = 8.0
    htf_trend_timeframe = "M15"


class Dir:
    def __init__(self, v): self.value = v


BUY, SELL = Dir("BUY"), Dir("SELL")


def target(trend, direction, cfg=None):
    fake = DualCrossConfirmedSwapEngine.__new__(DualCrossConfirmedSwapEngine)
    fake.config = cfg or Cfg()
    fake.current_htf_trend = trend
    return DualCrossConfirmedSwapEngine._take_profit_for(fake, direction)


def main() -> None:
    print("no stop loss")
    check("the guard that refused a null stop is gone",
          "requires stop_loss_usd to be set" not in ENGINE)
    check("a null stop distance yields NO stop price",
          "if distance is None:" in ENGINE and "return None" in ENGINE)
    check("a position with no stop can never be 'stopped out'",
          "stop_hit = position.stop_loss is not None and (" in ENGINE)
    check("the stop-hit message never formats a None distance",
          "if self.config.stop_loss_usd is not None else" in ENGINE)
    # Breakeven still sets a stop where one is configured -- removing the
    # base stop must not remove the ability to arm one later.
    check("breakeven can still arm a stop", "_breakeven_stop_price" in ENGINE)

    # There were TWO guards. Checking only the engine file passed while
    # bot/config.py still refused to LOAD a null stop, so the config was
    # written and the failure waited for the next restart -- which is worse
    # than failing at the moment of the edit.
    config_src = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
    check("bot/config.py does not refuse a null stop for the plain swap variant",
          "'dual_cross_confirmed_swap' but " not in config_src)
    check("the entryfilter variant still requires its stop (unchanged)",
          "'dual_cross_confirmed_swap_adx_entryfilter' but " in config_src)

    print("\ntrend-sized take-profit")
    check("BUY with an uptrend takes the bigger target", target(1.0, BUY)[0] == 8.0)
    check("SELL with a downtrend takes the bigger target", target(-1.0, SELL)[0] == 8.0)
    check("BUY against the trend keeps the normal target", target(-1.0, BUY)[0] == 6.0)
    check("SELL against the trend keeps the normal target", target(1.0, SELL)[0] == 6.0)
    check("an UNKNOWN trend keeps the normal target, never the bigger one",
          target(float("nan"), BUY)[0] == 6.0 and target(None, BUY)[0] == 6.0)

    class NoRule(Cfg):
        htf_trend_take_profit_usd = None
    check("an account without the rule is completely unaffected",
          target(1.0, BUY, NoRule())[0] == 6.0 and target(1.0, BUY, NoRule())[1] == "")

    check("the reason text says which target was chosen and why",
          "with the M15 trend" in target(1.0, BUY)[1]
          and "against the M15 trend" in target(-1.0, BUY)[1])

    print("\nread from the right candle")
    # The property is about ORDER INSIDE on_new_candle, not position in
    # the file: the trend must be set from THIS candle before any entry
    # can use it. Set at the end (like prev_ema13) it would be one candle
    # stale for the very entry it is meant to size.
    body = ENGINE[ENGINE.index("def on_new_candle"):]
    body = body[:body.index("\n    def ", 1)]
    check("on_new_candle reads the trend from this candle",
          "self.current_htf_trend = (" in body)
    check("and does so BEFORE any entry in the same call",
          body.index("self.current_htf_trend = (") < body.index("_maybe_enter_or_pend("))
    check("every entry logs the trend and the chosen target, for later analysis",
          "htf_trend=self.current_htf_trend" in ENGINE
          and "htf_aligned=" in ENGINE and "target_usd=" in ENGINE)

    print("\nlive/backtest parity")
    for name in ("main.py", "scripts/backtest.py", "scripts/fit_new_timeframe.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        check(f"{name} computes htf_trend",
              "compute_htf_trend(" in src)
        check(f"{name} guards it on config.htf_trend_timeframe",
              re.search(r"config\.htf_trend_timeframe is not None", src) is not None)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
