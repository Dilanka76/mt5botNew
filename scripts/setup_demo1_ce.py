"""One-time setup for the new demo1_ce (confirmed-entry) pseudo-account:
- config/settings.demo1_ce.yaml, based on demo1_m1's real config
- .env.demo1_ce, copied from .env.demo1_m1 (same MT5 login)
- adds demo1_ce's magic number (910005) to demo1_m1 and demo1_m3's
  sibling_magic_numbers, so reject_manual_trades doesn't fight between them
Does everything with explicit UTF-8 (no PowerShell Get-Content/Set-Content
involved) to avoid yesterday's encoding-corruption incident.
"""
import shutil
import yaml
from pathlib import Path

NEW_MAGIC = 910005

# --- 1. New account's settings, based on demo1_m1's real config ---
src = Path("config/settings.demo1_m1.yaml")
with open(src, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

raw["strategy_variant"] = "dual_cross_confirmed_entry"
raw.pop("dual_cross", None)
raw["dual_cross_confirmed_entry"] = {"closing_tolerance_usd": 0.02}
raw["sessions"]["dual_cross_confirmed_entry"] = raw["sessions"]["dual_cross"]
raw["execution"]["magic_number"] = NEW_MAGIC
raw["execution"]["sibling_magic_numbers"] = [910001, 910003]

out = Path("config/settings.demo1_ce.yaml")
with open(out, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Wrote {out}")

# --- 2. .env.demo1_ce, copied from .env.demo1_m1 (same MT5 login) ---
env_src = Path(".env.demo1_m1")
env_out = Path(".env.demo1_ce")
shutil.copy(env_src, env_out)
print(f"Wrote {env_out} (copied from {env_src})")

# --- 3. Add 910005 to demo1_m1 and demo1_m3's sibling_magic_numbers ---
for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        c = yaml.safe_load(f)
    current = c["execution"].get("sibling_magic_numbers", [])
    if NEW_MAGIC not in current:
        c["execution"]["sibling_magic_numbers"] = current + [NEW_MAGIC]
        with open(p, "w", encoding="utf-8") as f:
            yaml.dump(c, f, default_flow_style=False, sort_keys=False)
        print(f"Updated {p}: sibling_magic_numbers now {c['execution']['sibling_magic_numbers']}")
    else:
        print(f"{p}: {NEW_MAGIC} already present, no change")

# --- Verify everything loads cleanly via the real load_config() ---
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_ce", "demo1_m1", "demo1_m3"]:
    config = load_config(account)
    print(f"load_config('{account}') OK: strategy_variant={config.strategy_variant}, "
          f"magic={config.execution.magic_number}, siblings={config.execution.sibling_magic_numbers}")
