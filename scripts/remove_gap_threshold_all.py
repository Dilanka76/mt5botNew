"""One-off: makes EVERY confirmed cross enter immediately on ALL FOUR
accounts (demo1_m1, demo1_m3, demo2_m1, demo2_m3) -- explicit user
decision 2026-08-31. First requested for demo1_m1/demo1_m3 only
("both demo1 and 3 the gap 5$ and the 7$ ... need to be remove"), then
explicitly extended the same session to demo2 as well ("both demo
account").

Does NOT modify either engine
(state_machine_dual_cross_confirmed_swap_adx.py for demo1,
state_machine_dual_cross_confirmed_swap.py for demo2 -- both require
gap_threshold_usd to be set at all, constructor raises if it's None).
Instead sets gap_threshold_usd to an effectively-infinite value
(999999.0) on all four accounts: each engine's own
`gap < self.config.gap_threshold_usd` check then ALWAYS takes the
immediate-entry branch, so the EMA5-pullback path can structurally never
fire again on any of them -- same real effect as removing the rule,
achieved with a pure config change, fully reversible by setting the
numbers back later.

Context worth remembering if this is ever revisited: this removes exactly
the mechanism analyze_entry_quality.py/full_strategy_analysis.py found
working BEST on demo1_m3 specifically (ema5_touch entries: 86.7% win,
+$28.81 avg vs immediate's 69.6% win, +$13.10 avg, as of 2026-08-31) --
a real, still-standing finding, not one that was debunked (only a
*hypothesis about why m1 and m3 disagreed* was tested and failed, not the
underlying m3 result itself). Flagged to the user before running this;
explicit decision to proceed anyway, then explicitly widened to demo2.

Original per-account thresholds before this change: demo1_m1=$5.0 (never
changed), demo1_m3=$7.0 (raised from $5.0 on 2026-08-28, see
set_m3_gap7.py), demo2_m1=$5.0, demo2_m3=$7.0 (both set at demo2's
original build time).
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

EFFECTIVELY_INFINITE_GAP = 999999.0
ACCOUNTS = ("demo1_m1", "demo1_m3", "demo2_m1", "demo2_m3")

# Each account's stop_loss_usd/take_profit_usd, to confirm nothing else
# gets accidentally changed by this script.
EXPECTED_SL_TP = {
    "demo1_m1": (5.0, 5.0),
    "demo1_m3": (10.0, 6.0),
    "demo2_m1": (5.0, 5.0),
    "demo2_m3": (10.0, 6.0),
}

for account in ACCOUNTS:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    old_gap = raw.get("gap_threshold_usd")
    raw["gap_threshold_usd"] = EFFECTIVELY_INFINITE_GAP

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: gap_threshold_usd={old_gap} -> {EFFECTIVELY_INFINITE_GAP} (every confirmed cross now enters immediately)")

# Verify via the real load_config() before trusting it.
print()
for account in ACCOUNTS:
    config = load_config(account)
    expected_sl, expected_tp = EXPECTED_SL_TP[account]
    print(f"load_config('{account}'): gap_threshold_usd={config.gap_threshold_usd}, "
          f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}")
    assert config.gap_threshold_usd == EFFECTIVELY_INFINITE_GAP, f"{account}'s gap_threshold_usd is {config.gap_threshold_usd}, expected {EFFECTIVELY_INFINITE_GAP}!"
    assert config.stop_loss_usd == expected_sl, f"{account}'s stop_loss_usd changed unexpectedly to {config.stop_loss_usd}!"
    assert config.take_profit_usd == expected_tp, f"{account}'s take_profit_usd changed unexpectedly to {config.take_profit_usd}!"

print("\nConfirmed: gap threshold effectively removed on all four accounts, SL/TP unaffected on all four.")
