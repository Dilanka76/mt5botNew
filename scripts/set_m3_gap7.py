"""One-off: raises demo1_m3's (3-minute timeframe) gap_threshold_usd from
$5 to $7, matching demo2_m3's own gap threshold -- explicit user decision
2026-08-28.

Context, worth remembering if this ever gets revisited: scripts/
diff_trades_today.py's 2026-08-28 comparison found ONE trade that day
where demo1_m3 waited for an EMA5 pullback (gap=$5.52 >= its $5
threshold) while demo2_m3 entered immediately (same gap, under its own
$7 threshold). This is a small-sample decision (1 trade), not something
the evidence strongly demanded -- flagged to the user before making this
change, since it also runs slightly against the stronger, already-
validated finding in analyze_entry_quality.py that wide-gap/EMA5-
pullback entries outperform immediate entries (raising the threshold
means MORE gaps route to the immediate-entry path instead of pullback).
User's explicit call anyway ("i think the demo1 3m chrt ap 7$ is okay").
See [[project_demo1_demo2_comparison_log]] for the full context.

demo1_m1 is explicitly NOT touched by this script -- stays at its own
$5 gap threshold, untouched.
"""
import yaml
from pathlib import Path

p = Path("config/settings.demo1_m3.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old_gap = raw.get("gap_threshold_usd")
raw["gap_threshold_usd"] = 7.0

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: gap_threshold_usd={old_gap} -> 7.0")

# Verify via the real load_config() before trusting it.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

config = load_config("demo1_m3")
print(
    f"load_config('demo1_m3') OK: strategy_variant={config.strategy_variant}, "
    f"gap_threshold_usd={config.gap_threshold_usd}, "
    f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}"
)
assert config.gap_threshold_usd == 7.0, f"gap_threshold_usd is {config.gap_threshold_usd}, expected 7.0!"
assert config.stop_loss_usd == 10.0, f"stop_loss_usd unexpectedly changed to {config.stop_loss_usd} -- should still be 10.0!"
assert config.take_profit_usd == 6.0, f"take_profit_usd unexpectedly changed to {config.take_profit_usd} -- should still be 6.0!"
print("Confirmed: demo1_m3 gap_threshold_usd=7.0, stop_loss_usd/take_profit_usd unaffected (10.0/6.0)")

# Sanity check: demo1_m1 is untouched by this script.
m1_config = load_config("demo1_m1")
assert m1_config.gap_threshold_usd == 5.0, (
    f"demo1_m1's gap_threshold_usd unexpectedly changed to {m1_config.gap_threshold_usd} -- should still be 5.0!"
)
print(f"Confirmed: demo1_m1's config unaffected (gap_threshold_usd=5.0)")
