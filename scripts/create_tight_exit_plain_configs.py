"""One-off: creates BACKTEST-ONLY config copies of demo1_m1/demo1_m3 with
strategy_variant reverted to plain dual_cross_tight_exit (the OLD
strategy, before the swap-confirm fix). Needed because the REAL live
configs now run dual_cross_tight_exit_swap_confirm — this lets the OLD
strategy be backtested over the exact same window as the new strategy's
real trades, for a same-period comparison.

These files are NEVER read by main.py or the gateway — only by
scripts/backtest.py via --settings-path, e.g.:

    python scripts\\backtest.py --account demo1_m1 --settings-path config\\settings.demo1_m1.tight_exit_plain.yaml --from "2026-08-19 22:30" --to "<NOW>" --real-ticks
    python scripts\\backtest.py --account demo1_m3 --settings-path config\\settings.demo1_m3.tight_exit_plain.yaml --from "2026-08-19 22:30" --to "<NOW>" --real-ticks

The real config/settings.demo1_m1.yaml and settings.demo1_m3.yaml (and
therefore the live-running swap_confirm processes) are never touched by
this script.
"""
import yaml
from pathlib import Path

OLD_VARIANT = "dual_cross_tight_exit"

for account in ["demo1_m1", "demo1_m3"]:
    src = Path(f"config/settings.{account}.yaml")
    with open(src, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = OLD_VARIANT
    if OLD_VARIANT not in raw["sessions"]:
        # Reuse whatever session windows the swap_confirm section already
        # has (identical schedule, just needs the key to exist for this
        # variant name too).
        raw["sessions"][OLD_VARIANT] = raw["sessions"]["dual_cross_tight_exit_swap_confirm"]

    out = Path(f"config/settings.{account}.tight_exit_plain.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {out}")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account, settings_path=f"config/settings.{account}.tight_exit_plain.yaml")
    print(
        f"load_config('{account}', backtest-only path) OK: strategy_variant={config.strategy_variant}, "
        f"cross_tolerance_usd={config.dual_cross_tight_exit.cross_tolerance_usd}, "
        f"early_exit_usd={config.dual_cross_tight_exit.early_exit_usd}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}"
    )

# Sanity check the REAL live config is untouched.
for account in ["demo1_m1", "demo1_m3"]:
    live_config = load_config(account)
    assert live_config.strategy_variant == "dual_cross_tight_exit_swap_confirm", (
        f"{account}'s REAL live config was unexpectedly affected — this should never happen!"
    )
    print(f"Confirmed: {account}'s real live config unaffected (strategy_variant=dual_cross_tight_exit_swap_confirm)")
