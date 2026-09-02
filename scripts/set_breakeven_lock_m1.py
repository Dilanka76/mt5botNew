"""One-off: sets breakeven_lock_usd=0.5 on demo1_m1 and demo2_m1 ONLY
(M1 accounts) -- explicit user decision 2026-09-02: once breakeven arms
(floating profit reaches breakeven_trigger_usd, $4.50), lock in $0.50 of
real profit instead of moving the stop to exactly the entry price. A
reversal after arming now closes with a small guaranteed win instead of
exactly $0.

Requires the engine changes in bot/strategy/state_machine_dual_cross_
confirmed_swap_adx.py and bot/strategy/state_machine_dual_cross_confirmed_
swap.py (both already implement this -- breakeven_lock_usd was a new,
previously-nonexistent config field; None/unset means the original exact
-breakeven behavior, unchanged).

demo1_m3 and demo2_m3 are explicitly NOT touched -- M1 only, same scope
as breakeven_trigger_usd itself (demo1_m3/demo2_m3 have
breakeven_trigger_usd=None, so breakeven_lock_usd would be inert there
anyway, but this script still asserts they stay untouched for clarity).
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

BREAKEVEN_LOCK = 0.5
ACCOUNTS = ("demo1_m1", "demo2_m1")

for account in ACCOUNTS:
    p = Path(f"config/settings.{account}.yaml")
    with open(p, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)

    old_value = raw.get("breakeven_lock_usd")
    raw["breakeven_lock_usd"] = BREAKEVEN_LOCK

    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {p}: breakeven_lock_usd={old_value} -> {BREAKEVEN_LOCK}")

# Verify via the real load_config() before trusting it.
print()
for account in ACCOUNTS:
    config = load_config(account)
    print(f"load_config('{account}'): breakeven_trigger_usd={config.breakeven_trigger_usd}, "
          f"breakeven_lock_usd={config.breakeven_lock_usd}, take_profit_usd={config.take_profit_usd}")
    assert config.breakeven_lock_usd == BREAKEVEN_LOCK, f"{account}'s breakeven_lock_usd is {config.breakeven_lock_usd}, expected {BREAKEVEN_LOCK}!"
    assert config.breakeven_trigger_usd is not None, f"{account}'s breakeven_trigger_usd is unexpectedly None -- the lock is inert without a trigger!"

# Sanity check: demo1_m3/demo2_m3 are NOT touched.
for account in ("demo1_m3", "demo2_m3"):
    config = load_config(account)
    assert config.breakeven_lock_usd is None, f"{account}'s breakeven_lock_usd unexpectedly changed to {config.breakeven_lock_usd}!"
    print(f"Confirmed: {account} unaffected (breakeven_lock_usd={config.breakeven_lock_usd})")

print(f"\nConfirmed: breakeven lock enabled (+${BREAKEVEN_LOCK} guaranteed profit) on demo1_m1 and demo2_m1 only.")
