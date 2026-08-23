"""One-off migration: switches demo1_m1 and demo1_m3's REAL, LIVE
strategy_variant from dual_cross_confirmed_swap_adx to
dual_cross_confirmed_adx_m15 (entries gated by ADX(14)>=threshold; the
swap is REMOVED entirely and replaced with close-and-flatten on a single
confirmed opposite candle — see
state_machine_dual_cross_confirmed_adx_m15.py's module docstring for the
full design and the real-data reasoning behind it).

NOTE: this variant originally also required a higher timeframe (M15)
EMA13/21 agreement check alongside ADX -- removed the same day per
explicit user request ("15m look that one no need only adx"). The
strategy_variant name and this migration script were kept unchanged
(name continuity with the already-deployed live config), only the
engine's internal entry-gate logic changed.

Deployed WITHOUT a prior backtest, explicit user decision 2026-08-23 —
judged on live/demo results, same pattern as dual_cross_confirmed_swap_adx
was. See [[project_dual_cross_and_cross_confirmed]]'s "HOW TO REVERT to
dual_cross_confirmed_swap_adx" section if this needs to be rolled back —
scripts/switch_m1_m3_to_confirmed_swap_adx.py does exactly that.

Reuses the existing swap_adx_filter section unchanged (same
adx_period=14/adx_threshold=25.0 — the config field name is kept for
schema continuity even though this variant now gates entries with it
instead of a swap). gap_threshold_usd and take_profit_usd are existing
top-level fields, left untouched.

stop_loss_usd is explicitly changed from 15.0 to 10.0 for this variant
— explicit user decision 2026-08-23, alongside the design itself.
NOTE: stop_loss_usd is a shared top-level field, not variant-specific,
so reverting to dual_cross_confirmed_swap_adx via
scripts/switch_m1_m3_to_confirmed_swap_adx.py must also explicitly
restore it to 15.0 — that script does NOT touch stop_loss_usd itself
(never has), so without a manual edit a revert would silently keep the
$10 stop-loss on the old strategy too.

The old sessions key (dual_cross_confirmed_swap_adx) is left in the file
unchanged too -- simply unused by this engine, no need to remove it.

All edits use explicit UTF-8 (no PowerShell Get-Content/Set-Content), per
the lesson from the earlier encoding-corruption incident.
"""
import yaml
from pathlib import Path

OLD_VARIANT = "dual_cross_confirmed_swap_adx"
NEW_VARIANT = "dual_cross_confirmed_adx_m15"

for account in ["demo1_m1", "demo1_m3"]:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = NEW_VARIANT
    raw["sessions"][NEW_VARIANT] = raw["sessions"][OLD_VARIANT]
    # swap_adx_filter section already present from the previous variant --
    # left as-is (adx_period=14, adx_threshold=25.0), now read as the
    # entry gate instead of the swap gate.
    old_stop_loss = raw.get("stop_loss_usd")
    raw["stop_loss_usd"] = 10.0

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: strategy_variant={NEW_VARIANT}, stop_loss_usd={old_stop_loss} -> 10.0")

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

# Sanity check: live1 (untouched by this migration) is unaffected.
live_config = load_config("live1")
print(
    f"Confirmed: live1's config unaffected (strategy_variant={live_config.strategy_variant})"
)
