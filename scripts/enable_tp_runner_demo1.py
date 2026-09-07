"""Enables the TP-runner on demo1_m1 and demo1_m3 ONLY -- explicit user
decision 2026-09-07.

A trade reaching its take-profit is no longer closed. Shortly before
price arrives the broker take-profit is removed, at the target a REAL
broker-side stop is placed at the take-profit level, and from there the
stop follows the best price $2.00 behind, ratcheting up only. The
opposite-cross exit is unchanged.

Evidence (scripts/simulate_tp_runner.py, real trades since 2026-08-25,
on correct candles): lock-at-TP + $2.00 trail was positive on ALL FOUR
accounts in BOTH walk-forward halves -- demo1_m1 +$469.74, demo2_m1
+$318.00, demo1_m3 +$289.80, demo2_m3 +$338.88. It was the only idea
tested that week to clear a bar set BEFORE the results were seen; the
colour+volume filter, the Efficiency Ratio and the M3 breakeven all
failed it. About 9 of 10 trades end at exactly the old take-profit and
are unaffected -- the occasional runner produces the entire gain.

demo1_m3 ALSO gets breakeven_trigger_usd = $5.50 (lock $0.50). This is
not a profit rule, it is the safety net the TP-runner needs. The broker
take-profit has to be removed just before price reaches the target, and
in that ~1 second window the only protection is the stop below. Without
a breakeven there, demo1_m3's stop is still -$10.00, so a violent move
inside that window could turn a completed win into a full loss. M1
already has this cover at $4.50.

That $5.50 trigger passed walk-forward on demo1_m3 (+$71.16/+$22.08) but
FAILED on demo2_m3 (-$32.42/-$189.72), which is why it is not being
treated as a general improvement and is not going anywhere else.

Scope is deliberately narrow:
  - demo1_m1 and demo1_m3 only.
  - demo2_m1 and demo2_m3 stay UNTOUCHED as controls. demo2_m3 is the
    model for the live launch and must keep its proven history; without
    one unchanged account there is no way to tell a rule working from
    the market simply moving.

Two costs the simulation could NOT model, so treat +$470 as an upper
bound: slippage on the stop (the take-profit it replaces is a limit
order that cannot fill worse than its price), and entries missed while
a runner is still open.

    python scripts/enable_tp_runner_demo1.py

Then restart the demo1_m1 and demo1_m3 legs.
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from bot.config import load_config

TRAIL = 2.00
M3_BREAKEVEN_TRIGGER = 5.50
M3_BREAKEVEN_LOCK = 0.50
TARGETS = ("demo1_m1", "demo1_m3")
UNTOUCHED = ("demo2_m1", "demo2_m3")


def patch(account: str, updates: dict) -> None:
    path = Path(f"config/settings.{account}.yaml")
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)
    for key, value in updates.items():
        print(f"  {account}: {key} {raw.get(key)!r} -> {value!r}")
        raw[key] = value
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)


print("Enabling the TP-runner on demo1 only\n")
patch("demo1_m1", {"tp_runner_trail_usd": TRAIL})
patch("demo1_m3", {
    "tp_runner_trail_usd": TRAIL,
    "breakeven_trigger_usd": M3_BREAKEVEN_TRIGGER,
    "breakeven_lock_usd": M3_BREAKEVEN_LOCK,
})

# Verify through the real loader before trusting any of it.
print()
for account in TARGETS:
    c = load_config(account)
    assert c.tp_runner_trail_usd == TRAIL, f"{account}: trail did not take effect!"
    assert c.take_profit_usd is not None and c.stop_loss_usd is not None
    print(f"  {account}: TP ${c.take_profit_usd:.2f}, stop ${c.stop_loss_usd:.2f}, "
          f"trail ${c.tp_runner_trail_usd:.2f}, arm ${c.tp_runner_arm_before_usd:.2f} before target, "
          f"breakeven {c.breakeven_trigger_usd}")

m3 = load_config("demo1_m3")
assert m3.breakeven_trigger_usd == M3_BREAKEVEN_TRIGGER, "demo1_m3 breakeven safety net missing!"
assert m3.breakeven_trigger_usd < m3.take_profit_usd, (
    "the breakeven trigger must sit BELOW the target -- it exists to cover the window "
    "before the runner locks"
)

print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.tp_runner_trail_usd is None, (
        f"{account} has tp_runner_trail_usd={other.tp_runner_trail_usd}, expected None -- "
        f"demo2 must stay untouched as the control!"
    )
    print(f"  Confirmed: {account} untouched (tp_runner_trail_usd=None, "
          f"breakeven={other.breakeven_trigger_usd})")

print("\nTP-runner ON for demo1_m1 and demo1_m3. demo2 unchanged.")
print("Restart both demo1 legs for this to take effect.")
