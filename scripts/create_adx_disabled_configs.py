"""One-off: creates BACKTEST-ONLY config copies of demo1_m1/demo1_m3's
real, live-deployed dual_cross_confirmed_adx_m15 settings, with the ADX
entry gate effectively disabled (adx_threshold=0.0 -- ADX is never
negative, so this always passes unless the value is NaN, same fail-safe
behavior as today).

Purpose: answer "what would have happened to the signals ADX blocked" by
running a real-tick backtest with the gate off and comparing its trade
list against the real trades that actually happened. Every trade in the
ADX-off run that does NOT match a real trade corresponds to a signal
that was genuinely blocked live.

These files are NEVER read by main.py or the gateway — only by
scripts/backtest.py via --settings-path, e.g.:

    python scripts\\backtest.py --account demo1_m1 --settings-path config\\settings.demo1_m1.adxoff.yaml --from "2026-08-23 22:00" --to "2026-08-24 23:59" --real-ticks
    python scripts\\backtest.py --account demo1_m3 --settings-path config\\settings.demo1_m3.adxoff.yaml --from "2026-08-23 22:00" --to "2026-08-24 23:59" --real-ticks

The real config/settings.demo1_m1.yaml and settings.demo1_m3.yaml (and
therefore the live-running processes, still gated by real ADX) are never
touched by this script.
"""
import yaml
from pathlib import Path

for account in ["demo1_m1", "demo1_m3"]:
    src = Path(f"config/settings.{account}.yaml")
    with open(src, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    if raw.get("strategy_variant") != "dual_cross_confirmed_adx_m15":
        raise SystemExit(
            f"{src}: strategy_variant is {raw.get('strategy_variant')!r}, expected "
            f"'dual_cross_confirmed_adx_m15' -- refusing to build an ADX-off copy of "
            f"the wrong live strategy."
        )

    raw["swap_adx_filter"]["adx_threshold"] = 0.0

    out = Path(f"config/settings.{account}.adxoff.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {out}: adx_threshold=0.0 (gate effectively disabled)")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account, settings_path=f"config/settings.{account}.adxoff.yaml")
    print(
        f"load_config('{account}', backtest-only path) OK: strategy_variant={config.strategy_variant}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"gap_threshold_usd={config.gap_threshold_usd}, "
        f"adx_threshold={config.swap_adx_filter.adx_threshold}"
    )

# Sanity check the REAL live config is untouched.
for account in ["demo1_m1", "demo1_m3"]:
    live_config = load_config(account)
    assert live_config.swap_adx_filter.adx_threshold == 25.0, (
        f"{account}'s REAL live config was unexpectedly affected — this should never happen!"
    )
    print(f"Confirmed: {account}'s real live config unaffected (adx_threshold=25.0)")
