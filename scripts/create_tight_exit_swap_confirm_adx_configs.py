"""One-off: creates BACKTEST-ONLY config copies of demo1_m1/demo1_m3's
real, live-deployed settings for strategy_variant=dual_cross_tight_exit_swap_confirm_adx
(the "ADX swap gate" idea — see
bot/strategy/state_machine_dual_cross_tight_exit_swap_confirm_adx.py's
module docstring). Reuses each account's dual_cross_tight_exit section
($3 net, $15 stop) and session windows completely unchanged, adds a new
swap_adx_filter section (adx_period=14, adx_threshold=25.0 — the
defaults used throughout the real-data analysis this idea came from).
The only behavioral difference from the live swap_confirm strategy: a
2-candle-confirmed reversal swap additionally requires ADX(14) >= 25.0 at
that second confirming candle to actually fire.

These files are NEVER read by main.py or the gateway — only by
scripts/backtest.py via --settings-path, e.g.:

    python scripts\\backtest.py --account demo1_m1 --settings-path config\\settings.demo1_m1.tight_exit_swap_confirm_adx.yaml --from "2026-08-19 06:32" --to "2026-08-19 20:32" --real-ticks
    python scripts\\backtest.py --account demo1_m3 --settings-path config\\settings.demo1_m3.tight_exit_swap_confirm_adx.yaml --from "2026-08-19 06:32" --to "2026-08-19 20:32" --real-ticks

The real config/settings.demo1_m1.yaml and settings.demo1_m3.yaml (and
therefore the live-running processes) are never touched by this script.
"""
import yaml
from pathlib import Path

VARIANT = "dual_cross_tight_exit_swap_confirm_adx"

for account in ["demo1_m1", "demo1_m3"]:
    src = Path(f"config/settings.{account}.yaml")
    with open(src, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["strategy_variant"] = VARIANT
    raw["sessions"][VARIANT] = raw["sessions"]["dual_cross_tight_exit_swap_confirm"]
    raw["swap_adx_filter"] = {"adx_period": 14, "adx_threshold": 25.0}

    out = Path(f"config/settings.{account}.tight_exit_swap_confirm_adx.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {out}")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account, settings_path=f"config/settings.{account}.tight_exit_swap_confirm_adx.yaml")
    print(
        f"load_config('{account}', backtest-only path) OK: strategy_variant={config.strategy_variant}, "
        f"cross_tolerance_usd={config.dual_cross_tight_exit.cross_tolerance_usd}, "
        f"early_exit_usd={config.dual_cross_tight_exit.early_exit_usd}, "
        f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}, "
        f"adx_period={config.swap_adx_filter.adx_period}, adx_threshold={config.swap_adx_filter.adx_threshold}"
    )

# Sanity check the REAL live config is untouched.
for account in ["demo1_m1", "demo1_m3"]:
    live_config = load_config(account)
    assert live_config.strategy_variant == "dual_cross_tight_exit_swap_confirm", (
        f"{account}'s REAL live config was unexpectedly affected — this should never happen!"
    )
    print(f"Confirmed: {account}'s real live config unaffected (strategy_variant=dual_cross_tight_exit_swap_confirm)")
