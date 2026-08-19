"""One-off migration: switches demo1_m1 and demo1_m3 from strategy_variant
dual_cross_tight_exit to dual_cross_tight_exit_swap_confirm (the "swap
flip" fix — see state_machine_dual_cross_tight_exit_swap_confirm.py's
module docstring). Reuses the existing dual_cross_tight_exit config
section unchanged (cross_tolerance_usd=0.02, early_exit_usd=3.0 — the new
engine reads the same section, nothing to change there) and each
account's own session windows. take_profit_usd (demo1_m1=5.0,
demo1_m3=6.0) and stop_loss_usd (15.0, both) are untouched.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

NEW_VARIANT = "dual_cross_tight_exit_swap_confirm"

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = NEW_VARIANT
    raw["sessions"][NEW_VARIANT] = raw["sessions"]["dual_cross_tight_exit"]

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant={NEW_VARIANT}")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account)
    print(
        f"load_config('{account}') OK: strategy_variant={config.strategy_variant}, "
        f"cross_tolerance_usd={config.dual_cross_tight_exit.cross_tolerance_usd}, "
        f"early_exit_usd={config.dual_cross_tight_exit.early_exit_usd}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"sessions={config.sessions[NEW_VARIANT]}"
    )
