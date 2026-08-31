"""One-off: sets breakeven_trigger_usd=4.5 on demo1_m1 and demo2_m1 ONLY
(M1 accounts) -- explicit user decision 2026-08-31: "both demo account a
min, ... when the trade running and reachable the 5$ it comes the 4.5$
then the stop loss need to move the entry, only 1min".

Requires the engine changes in bot/strategy/state_machine_dual_cross_
confirmed_swap_adx.py and bot/strategy/state_machine_dual_cross_confirmed_
swap.py (both already implement this -- breakeven_trigger_usd was
previously a dormant, unused config field on both engines; now, once
floating profit reaches this many dollars, the stop-loss moves to the
entry price one-way, so a trade that nearly hits its $5 take-profit and
then reverses closes at breakeven instead of a full loss). Both M1
accounts have take_profit_usd=5.0, so a 4.5 trigger fires at 90% of the
way to TP.

demo1_m3 and demo2_m3 are explicitly NOT touched -- "only 1min".
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

BREAKEVEN_TRIGGER = 4.5
ACCOUNTS = ("demo1_m1", "demo2_m1")

for account in ACCOUNTS:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    old_value = raw.get("breakeven_trigger_usd")
    raw["breakeven_trigger_usd"] = BREAKEVEN_TRIGGER

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: breakeven_trigger_usd={old_value} -> {BREAKEVEN_TRIGGER}")

# Verify via the real load_config() before trusting it.
print()
for account in ACCOUNTS:
    config = load_config(account)
    print(f"load_config('{account}'): breakeven_trigger_usd={config.breakeven_trigger_usd}, "
          f"take_profit_usd={config.take_profit_usd}, stop_loss_usd={config.stop_loss_usd}")
    assert config.breakeven_trigger_usd == BREAKEVEN_TRIGGER, f"{account}'s breakeven_trigger_usd is {config.breakeven_trigger_usd}, expected {BREAKEVEN_TRIGGER}!"

# Sanity check: demo1_m3/demo2_m3 are NOT touched.
for account in ("demo1_m3", "demo2_m3"):
    config = load_config(account)
    assert config.breakeven_trigger_usd is None, f"{account}'s breakeven_trigger_usd unexpectedly changed to {config.breakeven_trigger_usd}!"
    print(f"Confirmed: {account} unaffected (breakeven_trigger_usd={config.breakeven_trigger_usd})")

print("\nConfirmed: breakeven-stop enabled ($4.50 trigger) on demo1_m1 and demo2_m1 only.")
