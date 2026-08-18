"""One-off: sets demo1_m3's take_profit_usd to 6.0 (was 5.0) — demo1_m3
only, demo1_m1 untouched."""
import yaml
from pathlib import Path

p = Path("config/settings.demo1_m3.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old_tp = raw["take_profit_usd"]
raw["take_profit_usd"] = 6.0

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: take_profit_usd {old_tp} -> 6.0")

import sys
sys.path.insert(0, ".")
from bot.config import load_config
config = load_config("demo1_m3")
print(f"load_config('demo1_m3') OK: take_profit_usd={config.take_profit_usd}")
