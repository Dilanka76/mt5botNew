"""One-off migration: switches demo1_m1 and demo1_m3's REAL, LIVE
strategy_variant from dual_cross_tight_exit_swap_confirm to
dual_cross_confirmed_swap_adx (confirmed-entry-only, no tick entry, no $3
net, 2-candle+ADX-gated swap, $5 gap/EMA5-pullback on flat entries — see
state_machine_dual_cross_confirmed_swap_adx.py's module docstring for the
full design). Deployed WITHOUT a prior backtest, explicit user decision
2026-08-20 — see the engine's module docstring.

Adds a new swap_adx_filter section (adx_period=14, adx_threshold=25.0)
and a new session key reusing the existing swap_confirm windows
unchanged. gap_threshold_usd, stop_loss_usd, and take_profit_usd are all
existing top-level fields, left completely untouched. The old
dual_cross_tight_exit section (if present) is left in the file
unchanged too — simply unused by this engine, no need to remove it.

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
    raw["sessions"][NEW_VARIANT] = raw["sessions"]["dual_cross_tight_exit_swap_confirm"]
    raw["swap_adx_filter"] = {"adx_period": 14, "adx_threshold": 25.0}

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
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"gap_threshold_usd={config.gap_threshold_usd}, "
        f"adx_period={config.swap_adx_filter.adx_period}, adx_threshold={config.swap_adx_filter.adx_threshold}, "
        f"sessions={config.sessions[NEW_VARIANT]}"
    )
