"""swap_immediate must not require an "adx" column to exist.

    python3 tests/test_adx_column_optional.py

On 2026-09-09 scripts/fit_new_timeframe.py died with KeyError: 'adx'
after 54 backtests had already started. The engine read

    adx_value = last_closed["adx"]

unconditionally, inside a branch entered whenever swap_immediate is on --
even though the very next line throws the value away:

    adx_ok = immediate or (...)

So the column was mandatory for a code path that never uses it. The live
bot survived only by accident: swap_adx_filter still happens to be set on
demo1, so main.py still computes the column. Clear that filter -- a
perfectly reasonable thing to do once the ADX gate is off -- and the
running bot crashes on its first reversal, mid-position.

That is the 2026-08-21 fault again: an engine reading a column the caller
was not required to supply, which that time disabled a stop-loss. See
feedback_live_backtest_data_parity.

Deliberately dependency-free: it reads the engine as TEXT instead of
importing it, so it runs on the Mac as well as the trading server. Every
other engine test needs pandas and can only run on the server, which is
where they stayed unrun for the whole of the change that caused this.
"""
from __future__ import annotations

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "bot" / "strategy" / \
    "state_machine_dual_cross_confirmed_swap_adx.py"

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> None:
    print("adx column must be optional when swap_immediate is on")
    src = ENGINE.read_text(encoding="utf-8")

    # 1. The read must be guarded by `immediate`, not unconditional.
    check("adx is read only on the debounced path",
          'adx_value = float("nan") if immediate else last_closed["adx"]' in src)
    check("the unconditional read is gone",
          'adx_value = last_closed["adx"]' not in src)

    # 2. A row with no "adx" key must not raise. This is the actual crash:
    #    pandas raises KeyError on a missing Series label.
    # A dict raises KeyError on a missing key exactly as a pandas Series
    # does for a missing label, which is the failure being reproduced.
    row = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
           "ema13": 1.4, "ema21": 1.3}
    check("the test row genuinely has no adx column", "adx" not in row)

    immediate = True
    try:
        adx_value = float("nan") if immediate else row["adx"]
        ok = True
    except KeyError:
        adx_value, ok = None, False
    check("the fixed expression evaluates without an adx column", ok)
    check("and yields NaN, which the immediate path never inspects",
          adx_value != adx_value)          # NaN is the only value != itself

    # The old expression must still fail, or this test proves nothing.
    try:
        _ = row["adx"]
        old_raised = False
    except KeyError:
        old_raised = True
    check("the OLD expression really would have raised (test is meaningful)", old_raised)

    # 3. The debounced path must be untouched -- it still needs real ADX.
    check("the debounced path still compares against the threshold",
          "self.config.swap_adx_filter.adx_threshold" in src)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
