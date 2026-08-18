"""One-off migration: switches demo1_m1 and demo1_m3 from strategy_variant
dual_cross to dual_cross_tight_exit — the new strategy built 2026-08-19
directly from real-trade analysis (see state_machine_dual_cross_tight_exit.py's
module docstring for the full design and rationale). Sets cross_tolerance_usd
to 0.02 (unchanged from dual_cross) and early_exit_usd to 3.0 (new, per
user's explicit design). take_profit_usd (demo1_m1=5.0, demo1_m3=6.0),
stop_loss_usd (15.0, both), and each account's own session windows are
left untouched — only the strategy-specific section and the top-level
variant name change.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = "dual_cross_tight_exit"
    raw.pop("dual_cross", None)
    raw["dual_cross_tight_exit"] = {"cross_tolerance_usd": 0.02, "early_exit_usd": 3.0}
    # Reuse the exact same session windows this account already had under
    # dual_cross (including any account-specific customization) — just
    # under the new variant's own sessions key.
    raw["sessions"]["dual_cross_tight_exit"] = raw["sessions"]["dual_cross"]

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant=dual_cross_tight_exit")

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
        f"sessions={config.sessions['dual_cross_tight_exit']}"
    )
