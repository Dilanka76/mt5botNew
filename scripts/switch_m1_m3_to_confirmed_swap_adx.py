"""One-off migration: switches demo1_m1 and demo1_m3's REAL, LIVE
strategy_variant from dual_cross_tight_exit_swap_confirm to
dual_cross_confirmed_swap_adx (confirmed-entry-only, no tick entry, no $3
net, 2-candle+ADX-gated swap, $5 gap/EMA5-pullback on flat entries — see
state_machine_dual_cross_confirmed_swap_adx.py's module docstring for the
full design). Deployed WITHOUT a prior backtest, explicit user decision
2026-08-20 — see the engine's module docstring.

Adds a new swap_adx_filter section (adx_period=14, adx_threshold=25.0)
and a new session key reusing the existing swap_confirm windows
unchanged (only if not already present — see REVERT note below).
gap_threshold_usd and take_profit_usd are existing top-level fields,
left completely untouched. The old dual_cross_tight_exit section (if
present) is left in the file unchanged too — simply unused by this
engine, no need to remove it.

REVERT NOTE (2026-08-24): this script is also the documented way to
revert BACK to this variant from dual_cross_confirmed_adx_m15 (see
[[project_dual_cross_and_cross_confirmed]]'s "HOW TO REVERT" section).
Re-running it is safe — the sessions[NEW_VARIANT] key is only written if
missing (it will already exist from the original 2026-08-21 deploy, left
untouched by every migration since). Explicitly restores
stop_loss_usd to 15.0, since dual_cross_confirmed_adx_m15's own
migration script (scripts/switch_m1_m3_to_confirmed_adx_m15.py)
tightened it to 10.0 and this shared top-level field is not otherwise
touched by any revert.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

NEW_VARIANT = "dual_cross_confirmed_swap_adx"

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = NEW_VARIANT
    if NEW_VARIANT not in raw["sessions"]:
        raw["sessions"][NEW_VARIANT] = raw["sessions"]["dual_cross_tight_exit_swap_confirm"]
    raw["swap_adx_filter"] = {"adx_period": 14, "adx_threshold": 25.0}
    old_stop_loss = raw.get("stop_loss_usd")
    raw["stop_loss_usd"] = 15.0

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant={NEW_VARIANT}, stop_loss_usd={old_stop_loss} -> 15.0")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account)
    print(
        f"load_config('{account}') OK: strategy_variant={config.strategy_variant}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"gap_threshold_usd={config.gap_threshold_usd}, "
        f"adx_period={config.swap_adx_filter.adx_period}, adx_threshold={config.swap_adx_filter.adx_threshold}, "
        f"sessions={config.sessions[NEW_VARIANT]}"
    )
