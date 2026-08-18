"""One-off migration: switches demo1_m1 and demo1_m3 from strategy_variant
dual_cross to dual_cross_confirmed_entry, per explicit user decision made
2026-08-18 despite the backtest showing dual_cross_confirmed_entry
underperforming dual_cross on the same 9-day period (-$377.10 vs +$244.02
on demo1_m1's own history) — the user wants to observe it live on both
accounts regardless. demo1_ce (the standalone test account for this same
strategy) is now redundant and is NOT touched by this script — retire it
separately with retire_demo1_ce.py.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = "dual_cross_confirmed_entry"
    raw.pop("dual_cross", None)
    raw["dual_cross_confirmed_entry"] = {"closing_tolerance_usd": 0.02}
    # Reuse the exact same session windows dual_cross already had for this
    # account (including any account-specific customization, e.g.
    # demo1_m1's added 16:00-19:00 window) — just under the new variant's
    # own sessions key.
    raw["sessions"]["dual_cross_confirmed_entry"] = raw["sessions"]["dual_cross"]

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant=dual_cross_confirmed_entry")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account)
    print(
        f"load_config('{account}') OK: strategy_variant={config.strategy_variant}, "
        f"closing_tolerance_usd={config.dual_cross_confirmed_entry.closing_tolerance_usd}, "
        f"sessions={config.sessions['dual_cross_confirmed_entry']}"
    )
