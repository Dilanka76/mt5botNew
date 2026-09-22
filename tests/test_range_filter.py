"""The range filter: the bot must see exactly what the research measured.

    python3 tests/test_range_filter.py

Added 2026-09-22. The research (scripts/range_forward_test.py) and the bot
(bot/indicators/range_filter.compute_range) both decide "is this entry
inside a trader-drawn M15 range?". If they ever disagreed, the forward
results would be measuring one rule while the bot traded another -- so
the first check replays a whole market through both and demands the same
answer on every candle.
"""
from __future__ import annotations

import importlib.util
import random
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

m = types.ModuleType("MetaTrader5")
m.__getattr__ = lambda name: 0                     # type: ignore[attr-defined]
sys.modules.setdefault("MetaTrader5", m)

import pandas as pd                                                           # noqa: E402

from bot.config import RangeFilterConfig                                      # noqa: E402
from bot.indicators.range_filter import compute_range, in_range, with_atr     # noqa: E402

spec = importlib.util.spec_from_file_location("rft", ROOT / "scripts/range_forward_test.py")
rft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rft)

CFG = RangeFilterConfig()
failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def market(seed: int, n: int = 2400) -> pd.DataFrame:
    """M3 candles: random walk with stretches of tight chop, so ranges form."""
    random.seed(seed)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    px, rows, idx = 4300.0, [], []
    for i in range(n):
        chop = (i // 200) % 2 == 1
        o = px
        px = (4300 + 3 * ((i % 20) - 10) / 10 + random.gauss(0, 0.4)) if chop else px + random.gauss(0, 0.9)
        rows.append((o, max(o, px) + abs(random.gauss(0, .3)), min(o, px) - abs(random.gauss(0, .3)), px))
        idx.append(start + timedelta(minutes=3 * i))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))


def m15(m3: pd.DataFrame) -> pd.DataFrame:
    return m3.resample("15min").agg({"open": "first", "high": "max",
                                     "low": "min", "close": "last"}).dropna()


def main() -> None:
    print("the bot and the research agree, candle by candle")
    for seed in (1, 2, 3):
        m3 = market(seed)
        htf = m15(m3)
        bot = compute_range(m3, htf, 15, 3, CFG.lookback, CFG.fractal, CFG.level_tolerance_atr)
        htf_atr = with_atr(htf)
        compared = disagreed = ranges = 0
        for t, row in bot.iterrows():
            close_time = t + timedelta(minutes=3)
            research = rft.range_at(htf_atr, close_time + timedelta(seconds=2), float(row["close"]))
            mine = in_range(row["close"], row["range_state"], row["range_ceiling"],
                            row["range_floor"], CFG)
            if research is None:
                continue
            compared += 1
            ranges += bool(research[0])
            if mine is not research[0]:
                disagreed += 1
        check(f"market {seed}: {compared} candles compared, {ranges} inside a range, "
              f"{disagreed} disagreements", compared > 1000 and ranges > 20 and disagreed == 0)

    print("\nno hindsight")
    m3 = market(1)
    htf = m15(m3)
    before = compute_range(m3, htf, 15, 3)
    cut = m3.index[1200]
    changed = htf.copy()
    changed.loc[changed.index > cut, ["high", "low"]] += 50.0     # rewrite the FUTURE
    after = compute_range(m3, changed, 15, 3)
    early = before.index + timedelta(minutes=3) <= cut
    same = (before.loc[early, "range_state"].fillna(-1) == after.loc[early, "range_state"].fillna(-1)).all()
    check("rewriting later M15 candles changes nothing that came before them", bool(same))

    print("\nthe verdict")
    check("inside the levels -> True", in_range(4305, 1.0, 4310, 4300, CFG) is True)
    check("above the ceiling -> False", in_range(4316, 1.0, 4310, 4300, CFG) is False)
    check("no range at all -> False", in_range(4305, 0.0, float("nan"), float("nan"), CFG) is False)
    check("not enough history -> None (trade as before)", in_range(4305, float("nan"), 1, 1, CFG) is None)
    check("filter off -> None", in_range(4305, 1.0, 4310, 4300, RangeFilterConfig(enabled=False)) is None)
    check("no config -> None", in_range(4305, 1.0, 4310, 4300, None) is None)
    check("too little M15 history -> all unknown",
          compute_range(m3.head(20), m15(m3.head(20)), 15, 3)["range_state"].isna().all())

    print("\nwired into both engines")
    for name in ("state_machine_dual_cross_confirmed_swap", "state_machine_dual_cross_confirmed_swap_adx"):
        src = (ROOT / "bot" / "strategy" / f"{name}.py").read_text(encoding="utf-8")
        check(f"{name}: both entry paths ask it", src.count("elif self._blocked_by_range(") == 2)
        check(f"{name}: every entry records it", "**self._range_check(candle)[1]" in src)
        check(f"{name}: a position still closes first (exits never blocked)",
              src.index('category="swapped') < src.index("elif self._blocked_by_range("))
    for path in ("main.py", "scripts/backtest.py"):
        check(f"{path} computes the columns (live/backtest parity)",
              "compute_range(" in (ROOT / path).read_text(encoding="utf-8"))

    print("\nrecord vs skip")
    import bot.strategy.state_machine_dual_cross_confirmed_swap as eng
    from bot.strategy.cross_detector import Direction
    logged = []
    eng.log_decision = lambda symbol, action, *a, **k: logged.append(action)
    e = object.__new__(eng.DualCrossConfirmedSwapStateMachine) if hasattr(
        eng, "DualCrossConfirmedSwapStateMachine") else object.__new__(
        next(v for v in vars(eng).values() if isinstance(v, type) and hasattr(v, "_blocked_by_range")))
    candle = pd.Series({"close": 4305.0, "range_state": 1.0, "range_ceiling": 4310.0, "range_floor": 4300.0})
    e.config = types.SimpleNamespace(symbol="XAUUSDp", range_filter=RangeFilterConfig(shadow_only=True))
    check("record-only: an in-range entry is still taken", e._blocked_by_range(Direction.BUY, candle, "t") is False)
    e.config.range_filter = RangeFilterConfig(shadow_only=False)
    check("skip: an in-range entry is withheld", e._blocked_by_range(Direction.BUY, candle, "t") is True)
    check("...and it says so", logged == ["entry_skipped_range"])
    bare = pd.Series({"close": 4305.0})
    check("skip, but no range columns -> trades as before", e._blocked_by_range(Direction.BUY, bare, "t") is False)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
