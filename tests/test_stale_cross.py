"""A failed entry must never turn into a trade on a LATER candle.

    python3 tests/test_stale_cross.py

REAL INCIDENT, demo2, 2026-09-21. The MT5 terminal's Algo Trading button
was off, so at 16:09 order_send failed:

    retcode=10027  comment='AutoTrading disabled by client'

on_new_candle() raised before reaching its last lines, where prev_ema13 /
prev_ema21 are updated. So every later candle compared against the
pre-16:09 EMAs and re-detected the SAME cross -- 148 errors in 30 minutes
-- and the moment the button came back on, the bot would have opened a
trade on a cross hours old: the late, extended entry that carries this
project's worst losses.

FIX: the engine records WHICH candle prev_ema belongs to. A cross whose
previous candle is more than one candle back is stale: logged as
entry_skipped_stale_cross and not entered. Retrying within the SAME
candle (exactly one candle back) is still allowed. Exits are not gated.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

m = types.ModuleType("MetaTrader5")
m.__getattr__ = lambda name: 0                        # type: ignore[attr-defined]
sys.modules.setdefault("MetaTrader5", m)

import bot.strategy.state_machine_dual_cross_confirmed_swap as plain_mod      # noqa: E402
import bot.strategy.state_machine_dual_cross_confirmed_swap_adx as adx_mod    # noqa: E402
from bot.strategy.cross_detector import Direction                            # noqa: E402

ENGINES = {
    "plain": (plain_mod, ROOT / "bot/strategy/state_machine_dual_cross_confirmed_swap.py"),
    "adx": (adx_mod, ROOT / "bot/strategy/state_machine_dual_cross_confirmed_swap_adx.py"),
}
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def engine(mod, prev_time, with_bar=True):
    cls = next(v for v in vars(mod).values()
               if isinstance(v, type) and hasattr(v, "_cross_is_stale") and v.__module__ == mod.__name__)
    e = object.__new__(cls)
    e.config = types.SimpleNamespace(symbol="XAUUSDp", timeframe="M3")
    if with_bar:
        e._bar = timedelta(minutes=3)
    e.prev_candle_time = prev_time
    return e


def main() -> None:
    t0 = datetime(2026, 9, 21, 13, 6, tzinfo=timezone.utc)      # 16:09 broker-ish candle
    for name, (mod, path) in ENGINES.items():
        logged: list[str] = []
        mod.log_decision = lambda symbol, action, *a, **k: logged.append(action)

        print(f"\n{name}: what counts as stale")
        check(f"{name}: no previous candle yet -> not stale (warm-up covers startup)",
              engine(mod, None)._cross_is_stale(Direction.BUY, t0) is False)
        check(f"{name}: retrying the SAME candle -> allowed",
              engine(mod, t0 - timedelta(minutes=3))._cross_is_stale(Direction.BUY, t0) is False)
        check(f"{name}: the very next candle -> allowed",
              engine(mod, t0)._cross_is_stale(Direction.BUY, t0 + timedelta(minutes=3)) is False)
        logged.clear()
        check(f"{name}: two candles later -> STALE, not entered",
              engine(mod, t0)._cross_is_stale(Direction.BUY, t0 + timedelta(minutes=6)) is True)
        check(f"{name}: ...and it says so", logged == ["entry_skipped_stale_cross"])
        check(f"{name}: hours later (the incident) -> STALE",
              engine(mod, t0)._cross_is_stale(Direction.SELL, t0 + timedelta(hours=4)) is True)
        check(f"{name}: an old test fixture without _bar is left alone",
              engine(mod, t0, with_bar=False)._cross_is_stale(Direction.BUY, t0 + timedelta(hours=4)) is False)

        print(f"\n{name}: wired into the engine")
        src = path.read_text(encoding="utf-8")
        check(f"{name}: both entry paths ask it", src.count("elif self._cross_is_stale(") == 2)
        check(f"{name}: the candle is recorded next to prev_ema",
              "        self.prev_ema21 = ema21\n        self.prev_candle_time = last_closed_time\n" in src)
        body = src[src.index("    def on_new_candle"):src.index("        self.prev_candle_time = last_closed_time")]
        check(f"{name}: on_new_candle has no early return that would skip recording it",
              not any(line.strip().startswith("return") for line in body.splitlines()))
        check(f"{name}: an open position still closes before the gate (exits never blocked)",
              src.index('category="swapped') < src.index("elif self._cross_is_stale("))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
