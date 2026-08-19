"""One-off: creates BACKTEST-ONLY config copies of demo1_m1/demo1_m3's
real, live-deployed settings for strategy_variant=dual_cross_tight_exit_gap_ema5
(see bot/strategy/state_machine_dual_cross_tight_exit_gap_ema5.py's module
docstring for the full design). Sets gap_threshold_usd to 5.0 (the user's
example value) and reuses each account's dual_cross_tight_exit section
and session windows unchanged.

These files are NEVER read by main.py or the gateway — only by
scripts/backtest.py via --settings-path, e.g.:

    python scripts\\backtest.py --account demo1_m1 --settings-path config\\settings.demo1_m1.tight_exit_gap_ema5.yaml --from 2026-08-12 --to 2026-08-19 --real-ticks
    python scripts\\backtest.py --account demo1_m3 --settings-path config\\settings.demo1_m3.tight_exit_gap_ema5.yaml --from 2026-08-12 --to 2026-08-19 --real-ticks

The real config/settings.demo1_m1.yaml and settings.demo1_m3.yaml (and
therefore the live-running processes) are never touched by this script.
"""
import yaml
from pathlib import Path

GAP_THRESHOLD_USD = 5.0

for account in ["demo1_m1", "demo1_m3"]:
    src = Path(f"config/settings.{account}.yaml")
    with open(src, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = "dual_cross_tight_exit_gap_ema5"
    raw["gap_threshold_usd"] = GAP_THRESHOLD_USD
    raw["sessions"]["dual_cross_tight_exit_gap_ema5"] = raw["sessions"]["dual_cross_tight_exit"]

    out = Path(f"config/settings.{account}.tight_exit_gap_ema5.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {out}")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account, settings_path=f"config/settings.{account}.tight_exit_gap_ema5.yaml")
    print(
        f"load_config('{account}', backtest-only path) OK: "
        f"strategy_variant={config.strategy_variant}, gap_threshold_usd={config.gap_threshold_usd}, "
        f"cross_tolerance_usd={config.dual_cross_tight_exit.cross_tolerance_usd}, "
        f"early_exit_usd={config.dual_cross_tight_exit.early_exit_usd}, "
        f"sessions={config.sessions['dual_cross_tight_exit_gap_ema5']}"
    )

# Sanity check the REAL live config is untouched.
for account in ["demo1_m1", "demo1_m3"]:
    live_config = load_config(account)
    assert live_config.strategy_variant == "dual_cross_tight_exit", (
        f"{account}'s REAL live config was unexpectedly affected — this should never happen!"
    )
    print(f"Confirmed: {account}'s real live config unaffected (strategy_variant=dual_cross_tight_exit)")
