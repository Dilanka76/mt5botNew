"""One-off: sets demo1_m1's (1-minute timeframe) stop_loss_usd to $5.00,
explicit user decision 2026-08-24 -- tighter stop for the faster M1
timeframe than M3's. demo1_m3 stays at $15.00, untouched by this script
(explicitly verified below).

stop_loss_usd is a shared top-level config field, not variant-specific
-- this changes it for WHATEVER strategy_variant is active on demo1_m1
at the time this runs (currently dual_cross_confirmed_swap_adx).
"""
import yaml
from pathlib import Path

p = Path("config/settings.demo1_m1.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old_stop_loss = raw.get("stop_loss_usd")
raw["stop_loss_usd"] = 5.0

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: stop_loss_usd={old_stop_loss} -> 5.0")

# Verify via the real load_config() before trusting it.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

config = load_config("demo1_m1")
print(
    f"load_config('demo1_m1') OK: strategy_variant={config.strategy_variant}, "
    f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}"
)

# Sanity check: demo1_m3 is untouched by this script.
m3_config = load_config("demo1_m3")
assert m3_config.stop_loss_usd == 15.0, (
    f"demo1_m3's stop_loss_usd unexpectedly changed to {m3_config.stop_loss_usd} -- should still be 15.0!"
)
print(f"Confirmed: demo1_m3's config unaffected (stop_loss_usd=15.0)")
