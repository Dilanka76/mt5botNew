"""One-off: makes EVERY confirmed cross enter immediately on demo1_m1 AND
demo1_m3 -- explicit user decision 2026-08-31, "both demo1 and 3 the gap
5$ and the 7$ these are need to be remove every cross confirmed need to be
enter direct".

Does NOT modify the engine (state_machine_dual_cross_confirmed_swap_adx.py
requires gap_threshold_usd to be set at all -- its constructor raises if
it's None). Instead sets gap_threshold_usd to an effectively-infinite
value (999999.0) on both accounts: _maybe_enter_or_pend's own
`gap < self.config.gap_threshold_usd` check then ALWAYS takes the
immediate-entry branch, so the EMA5-pullback path can structurally never
fire again -- same real effect as removing the rule, achieved with a pure
config change, fully reversible by setting the number back later.

Context worth remembering if this is ever revisited: this removes exactly
the mechanism analyze_entry_quality.py/full_strategy_analysis.py found
working BEST on demo1_m3 specifically (ema5_touch entries: 86.7% win,
+$28.81 avg vs immediate's 69.6% win, +$13.10 avg, as of 2026-08-31) --
a real, still-standing finding, not one that was debunked (only a
*hypothesis about why m1 and m3 disagreed* was tested and failed, not the
underlying m3 result itself). Flagged to the user before running this;
explicit decision to proceed anyway.

demo1_m1's own gap_threshold_usd was $5.0 (never changed); demo1_m3's was
$7.0 (raised from $5.0 on 2026-08-28, see set_m3_gap7.py). demo2_m1/
demo2_m3 are NOT touched by this script.
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

EFFECTIVELY_INFINITE_GAP = 999999.0

for account in ("demo1_m1", "demo1_m3"):
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    old_gap = raw.get("gap_threshold_usd")
    raw["gap_threshold_usd"] = EFFECTIVELY_INFINITE_GAP

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: gap_threshold_usd={old_gap} -> {EFFECTIVELY_INFINITE_GAP} (every confirmed cross now enters immediately)")

# Verify via the real load_config() before trusting it.
m1_config = load_config("demo1_m1")
m3_config = load_config("demo1_m3")
print(f"\nload_config('demo1_m1'): gap_threshold_usd={m1_config.gap_threshold_usd}, "
      f"stop_loss_usd={m1_config.stop_loss_usd}, take_profit_usd={m1_config.take_profit_usd}")
print(f"load_config('demo1_m3'): gap_threshold_usd={m3_config.gap_threshold_usd}, "
      f"stop_loss_usd={m3_config.stop_loss_usd}, take_profit_usd={m3_config.take_profit_usd}")
assert m1_config.gap_threshold_usd == EFFECTIVELY_INFINITE_GAP
assert m3_config.gap_threshold_usd == EFFECTIVELY_INFINITE_GAP
assert m1_config.stop_loss_usd == 5.0 and m1_config.take_profit_usd == 5.0, "demo1_m1's SL/TP changed unexpectedly!"
assert m3_config.stop_loss_usd == 10.0 and m3_config.take_profit_usd == 6.0, "demo1_m3's SL/TP changed unexpectedly!"
print("Confirmed: gap threshold effectively removed on both, SL/TP unaffected on both.")

# Sanity check: demo2 accounts are NOT touched by this script.
d2m1 = load_config("demo2_m1")
d2m3 = load_config("demo2_m3")
assert d2m1.gap_threshold_usd == 5.0, f"demo2_m1's gap_threshold_usd unexpectedly changed to {d2m1.gap_threshold_usd}!"
assert d2m3.gap_threshold_usd == 7.0, f"demo2_m3's gap_threshold_usd unexpectedly changed to {d2m3.gap_threshold_usd}!"
print(f"Confirmed: demo2_m1 (gap={d2m1.gap_threshold_usd}) and demo2_m3 (gap={d2m3.gap_threshold_usd}) unaffected.")
