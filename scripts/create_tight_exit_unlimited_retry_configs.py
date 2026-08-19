"""One-off: creates backtest-ONLY config copies of demo1_m1/demo1_m3's
real, live-deployed settings, with dual_cross_tight_exit.
allow_multiple_tick_attempts_per_candle set True — reproducing an earlier
draft of the engine that allowed unlimited same-candle tick-based retries
after each early-exit hit, for comparison against the corrected
one-attempt-per-candle behavior. Requested by the user 2026-08-19.

These files are NEVER read by main.py or the gateway — only by
scripts/backtest.py via --settings-path, e.g.:

    python scripts\\backtest.py --account demo1_m1 --settings-path config\\settings.demo1_m1.tight_exit_unlimited_retry.yaml --from 2026-08-12 --to 2026-08-19 --real-ticks
    python scripts\\backtest.py --account demo1_m3 --settings-path config\\settings.demo1_m3.tight_exit_unlimited_retry.yaml --from 2026-08-12 --to 2026-08-19 --real-ticks

The real config/settings.demo1_m1.yaml and settings.demo1_m3.yaml (and
therefore the live-running processes) are never touched by this script.
"""
import yaml
from pathlib import Path

for account in ["demo1_m1", "demo1_m3"]:
    src = Path(f"config/settings.{account}.yaml")
    with open(src, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    raw["dual_cross_tight_exit"]["allow_multiple_tick_attempts_per_candle"] = True

    out = Path(f"config/settings.{account}.tight_exit_unlimited_retry.yaml")
    with open(out, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Wrote {out}")

# Verify both load cleanly via the real load_config() before trusting them.
import sys
sys.path.insert(0, ".")
from bot.config import load_config

for account in ["demo1_m1", "demo1_m3"]:
    config = load_config(account, settings_path=f"config/settings.{account}.tight_exit_unlimited_retry.yaml")
    print(
        f"load_config('{account}', backtest-only path) OK: "
        f"allow_multiple_tick_attempts_per_candle={config.dual_cross_tight_exit.allow_multiple_tick_attempts_per_candle}, "
        f"cross_tolerance_usd={config.dual_cross_tight_exit.cross_tolerance_usd}, "
        f"early_exit_usd={config.dual_cross_tight_exit.early_exit_usd}"
    )

# Sanity check the REAL live config is untouched (still False/absent).
for account in ["demo1_m1", "demo1_m3"]:
    live_config = load_config(account)
    assert live_config.dual_cross_tight_exit.allow_multiple_tick_attempts_per_candle is False, (
        f"{account}'s REAL live config was unexpectedly affected — this should never happen!"
    )
    print(f"Confirmed: {account}'s real live config unaffected (allow_multiple_tick_attempts_per_candle=False)")
