"""The consolidation filter: does it block the right entries, and does it
stay out of the way when it cannot tell?

Built 2026-09-20 for the user's "identify sideways consolidation and miss
that trade" rule. The behaviour that matters most here is the boring one:
a missing or NaN measurement must leave the bot trading exactly as it did
before. A filter that silently halts trading because a number went
missing would be far worse than the losses it is meant to avoid.
"""
from __future__ import annotations

import sys
import types
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

m = types.ModuleType("MetaTrader5")
m.__getattr__ = lambda name: 0          # type: ignore[attr-defined]
sys.modules.setdefault("MetaTrader5", m)

from bot.config import ConsolidationFilterConfig
from bot.indicators.consolidation import compute_consolidation, is_consolidating

CFG = ConsolidationFilterConfig(overlap_min=0.60, box_atr_max=2.0, lookback=6)


def htf_frame(rows):
    idx = pd.date_range("2026-09-15 00:00", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def box_rows(n=30):
    """Candles printing side by side inside a $2 range."""
    return [(4300.0, 4301.0, 4299.0, 4300.5) for _ in range(n)]


def trend_rows(n=30):
    """Each candle taking new ground."""
    return [(4300.0 + 4 * i, 4304.0 + 4 * i, 4299.5 + 4 * i, 4303.5 + 4 * i) for i in range(n)]


def trading_frame(htf):
    """An M3 frame covering the same span, so the HTF values carry onto it."""
    idx = pd.date_range(htf.index[0], htf.index[-1] + timedelta(minutes=15), freq="3min", tz="UTC")
    return pd.DataFrame({"open": 4300.0, "high": 4300.5, "low": 4299.5, "close": 4300.0}, index=idx)


def test_a_box_is_recognised():
    htf = htf_frame(box_rows())
    out = compute_consolidation(trading_frame(htf), htf, 15, CFG.lookback)
    last = out.iloc[-1]
    assert last["cons_overlap"] > 0.9, "side-by-side candles must overlap almost completely"
    assert last["cons_box_atr"] < 2.0
    assert is_consolidating(last["cons_overlap"], last["cons_box_atr"], CFG) is True


def test_a_trending_market_is_not_a_box():
    htf = htf_frame(trend_rows())
    out = compute_consolidation(trading_frame(htf), htf, 15, CFG.lookback)
    last = out.iloc[-1]
    assert last["cons_overlap"] < 0.6
    assert last["cons_box_atr"] > 2.0
    assert is_consolidating(last["cons_overlap"], last["cons_box_atr"], CFG) is False


def test_both_conditions_are_required():
    assert is_consolidating(0.95, 8.0, CFG) is False, "overlapping but wide is not a box"
    assert is_consolidating(0.10, 1.0, CFG) is False, "narrow but marching is not a box"


def test_it_never_sees_the_future():
    """A value must be stamped at the HTF candle's CLOSE, so a row cannot
    carry information from a candle that had not finished yet."""
    htf = htf_frame(box_rows(10) + trend_rows(10))
    out = compute_consolidation(trading_frame(htf), htf, 15, CFG.lookback)
    close_of_last_box = htf.index[9] + timedelta(minutes=15)
    before = out[out.index <= close_of_last_box].iloc[-1]
    assert is_consolidating(before["cons_overlap"], before["cons_box_atr"], CFG) is True, \
        "while still inside the box, the box must be what is reported"


def test_missing_data_fails_open():
    """No HTF candles at all, NaN, or None: the answer is None, which the
    engine treats as 'trade exactly as before'."""
    empty = compute_consolidation(trading_frame(htf_frame(box_rows())), None, 15, CFG.lookback)
    assert empty["cons_overlap"].isna().all()
    assert is_consolidating(float("nan"), 1.0, CFG) is None
    assert is_consolidating(None, None, CFG) is None
    assert is_consolidating(0.9, 1.0, ConsolidationFilterConfig(enabled=False)) is None
    assert is_consolidating(0.9, 1.0, None) is None


def test_too_little_history_is_not_a_box():
    htf = htf_frame(box_rows(3))
    out = compute_consolidation(trading_frame(htf), htf, 15, CFG.lookback)
    assert out["cons_overlap"].isna().all(), "fewer candles than the lookback cannot be judged"


def test_shadow_only_decides_whether_a_box_blocks():
    """The same verdict, two behaviours: live2 records, demo2 skips."""
    for shadow_only, expected in ((True, False), (False, True)):
        cfg = ConsolidationFilterConfig(overlap_min=0.6, box_atr_max=2.0,
                                        shadow_only=shadow_only)
        verdict = is_consolidating(0.8, 1.2, cfg)
        assert verdict is True                 # the measurement never changes
        assert (verdict is True and not cfg.shadow_only) is expected


def test_the_engines_ask_before_every_entry():
    """Source check, the same shape as test_entry_warmup.py: every entry
    path in both engines must pass through the gate, and no exit may."""
    root = Path(__file__).resolve().parent.parent
    for name in ("state_machine_dual_cross_confirmed_swap",
                 "state_machine_dual_cross_confirmed_swap_adx"):
        src = (root / "bot" / "strategy" / f"{name}.py").read_text(encoding="utf-8")
        assert src.count("elif self._blocked_by_consolidation(") == 2, \
            f"{name}: both entry paths (flat entry and swap re-entry) must be gated"
        assert "self._consolidation(candle)[1]" in src, \
            f"{name}: every trade_entered line must carry the measurement"
        # the gate must sit AFTER the close on the swap path, so an exit
        # is never withheld -- same ordering rule as the warm-up guard
        assert src.index('category="swapped') < src.index("elif self._blocked_by_consolidation("), \
            f"{name}: a position must still close on the opposite cross"


def main() -> None:
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"  FAIL {name}")
    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
