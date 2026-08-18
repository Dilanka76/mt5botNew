"""One-off migration: switches demo1_m1 and demo1_m3 back from
strategy_variant dual_cross_confirmed_entry to dual_cross (their original
variant before the 2026-08-18 switch). Restores the 'dual_cross:' section
(cross_tolerance_usd=0.02, the value both accounts ran with previously)
that the earlier switch script popped. Leaves the
'dual_cross_confirmed_entry:' section and take_profit_usd values
(demo1_m1=5.0, demo1_m3=6.0) untouched — dormant/unrelated to this
variant. sessions['dual_cross'] was never removed by the earlier switch,
so no session changes needed here.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = "dual_cross"
    raw["dual_cross"] = {"cross_tolerance_usd": 0.02}

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant=dual_cross")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account)
    print(
        f"load_config('{account}') OK: strategy_variant={config.strategy_variant}, "
        f"cross_tolerance_usd={config.dual_cross.cross_tolerance_usd}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"sessions={config.sessions['dual_cross']}"
    )
