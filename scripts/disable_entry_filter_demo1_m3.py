"""One-off: DISABLES the entry-quality filter on demo1_m3, reversing
scripts/set_entry_filter_demo1_m3.py -- explicit user decision
2026-09-07 after the evidence that justified it turned out to be void.

Why it is being switched off:

1. The evidence it was deployed on was WRONG. The forward figures in
   set_entry_filter_demo1_m3.py's docstring ("passing both checks won
   82.4%") were produced by scripts that matched each trade to the wrong
   candle -- 238 of 245 entries, 97% (see bot/strategy/cross_lookup.py
   and scripts/audit_backtest_candle_matching.py). They described
   candles the bot never acted on.

2. The corrected backtest fails walk-forward. On demo1_m3 the full
   sample looks strong (+$706.80) but every variant LOSES in the first
   half and only earns in the second: colour-only -$130.02/+$265.80,
   volume-only -$119.46/+$472.44, combined -$35.64/+$742.44. The gain
   sits entirely in demo1_m3's losing stretch, where any trade-removing
   rule prints a profit without predicting anything.

3. Live forward results agree, and this evidence is clean -- it comes
   from the engine's own logged `entry_filtered` decisions and never
   depended on the broken picker. Of 8 real blocks, only 2 were right:
   avoided $240 of losses, missed $432 of wins, net -$192. With a $10
   stop and $6 target the filter needed only a 37.5% hit rate to break
   even (72/(72+120)); it ran at 25%. Tick volume drove 6 of the 8
   blocks and was wrong on 4 of those.

Shadow logging is NOT affected -- the engine still computes and logs
shadow_closed_in_favor / shadow_low_volume on every entry, so both ideas
keep accumulating free forward evidence. Only the acting-on-them stops.

demo2_m3 was never given this filter and stays untouched, as does every
other leg.

    python scripts/disable_entry_filter_demo1_m3.py

Then restart the demo1_m3 leg for it to take effect.
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

TARGET = "demo1_m3"
UNTOUCHED = ("demo1_m1", "demo2_m1", "demo2_m3")

p = Path(f"config/settings.{TARGET}.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old = raw.get("entry_filter_enabled")
raw["entry_filter_enabled"] = False

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: entry_filter_enabled={old} -> False")

# Verify through the real loader before trusting it.
print()
config = load_config(TARGET)
print(f"load_config('{TARGET}'): entry_filter_enabled={config.entry_filter_enabled}, "
      f"strategy_variant={config.strategy_variant}, timeframe={config.timeframe}, "
      f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}")
assert config.entry_filter_enabled is False, "entry_filter_enabled did not switch off!"

# Nothing else may have changed. Every account should now be filter-free.
print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.entry_filter_enabled is False, (
        f"{account}'s entry_filter_enabled is {other.entry_filter_enabled}, expected False!"
    )
    print(f"Confirmed: {account} unaffected (entry_filter_enabled={other.entry_filter_enabled})")

print(f"\nConfirmed: entry filter OFF on {TARGET}. No account now filters entries.")
print("Restart the demo1_m3 leg for this to take effect.")
