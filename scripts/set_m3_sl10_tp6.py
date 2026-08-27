"""One-off: rebalances demo1_m3's (3-minute timeframe) risk:reward ratio
-- explicit user decision 2026-08-27, paired change:

  stop_loss_usd:   15.0 -> 10.0
  take_profit_usd:  5.0 -> 6.0

Old ratio 15:5 (3:1) needed a 75% win rate just to break even. New ratio
10:6 (1.67:1) only needs 62.5% -- demo1_m3's real win rate so far
(87.5% across 16 trades) comfortably clears both, but the new ratio
leaves far more room if the win rate drops after this change. Real
losses on demo1_m3 so far have averaged -$73.50 (only 2, but both near
the full $15 stop) -- this directly targets reducing that loss size.

demo1_m1 is explicitly NOT touched by this script -- stays at its own
$5 SL / $5 TP, untouched.

Judge on real results over the next 10-15 demo1_m3 trades before
deciding to keep, adjust, or revert -- same standing principle as every
other change tonight (see [[project_dual_cross_and_cross_confirmed]]
and [[project_swap_stop_tighten_trade_log]] for the full history).
"""
import yaml
from pathlib import Path

p = Path("config/settings.demo1_m3.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old_stop_loss = raw.get("stop_loss_usd")
old_take_profit = raw.get("take_profit_usd")
raw["stop_loss_usd"] = 10.0
raw["take_profit_usd"] = 6.0

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: stop_loss_usd={old_stop_loss} -> 10.0, take_profit_usd={old_take_profit} -> 6.0")

# Verify via the real load_config() before trusting it.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

config = load_config("demo1_m3")
print(
    f"load_config('demo1_m3') OK: strategy_variant={config.strategy_variant}, "
    f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}"
)
assert config.stop_loss_usd == 10.0, f"stop_loss_usd is {config.stop_loss_usd}, expected 10.0!"
assert config.take_profit_usd == 6.0, f"take_profit_usd is {config.take_profit_usd}, expected 6.0!"
print("Confirmed: demo1_m3 stop_loss_usd=10.0 and take_profit_usd=6.0")

# Sanity check: demo1_m1 is untouched by this script.
m1_config = load_config("demo1_m1")
assert m1_config.stop_loss_usd == 5.0, (
    f"demo1_m1's stop_loss_usd unexpectedly changed to {m1_config.stop_loss_usd} -- should still be 5.0!"
)
assert m1_config.take_profit_usd == 5.0, (
    f"demo1_m1's take_profit_usd unexpectedly changed to {m1_config.take_profit_usd} -- should still be 5.0!"
)
print(f"Confirmed: demo1_m1's config unaffected (stop_loss_usd=5.0, take_profit_usd=5.0)")