"""demo1_m3 only: lock the TP-runner $1 BELOW the target, trail $0.50.

User's proposal 2026-09-08, tested and adopted. Currently the stop locks
exactly at the $6.00 target, so the dip that normally follows the target
stops the trade out immediately -- six of the first seven real locks did
exactly that. Locking at $5.00 gives the trade room to survive that dip
and go on to run.

Evidence (scripts/simulate_tp_runner.py, real candles since 2026-08-25),
combined across both M3 accounts:
    lock $6.00 + trail $2.00 (current)  +$984
    lock $5.00 + trail $0.50 (this)   +$1,211
Passes both walk-forward halves on BOTH M3 legs -- demo1_m3
+$340.56/+$280.02, demo2_m3 +$147.28/+$443.52.

WHY THE TIGHT TRAIL GOES WITH IT: the lock and the trail both supply
breathing room, and paying for both is waste. With $1 of cushion at the
lock the trail's only job is to KEEP what a run earned, so it should be
tight. Same lock with the old $2.00 trail scores +$873 -- worse than
changing nothing.

WHY M1 IS NOT CHANGED: on M1 the same $1 is 20% of a $5 target and its
moves are too short to earn it back -- it LOST money and failed
walk-forward on demo1_m3's M1 sibling. A tighter trail alone was also
tested there and came out mixed (+$71 on demo1_m1, -$21 on demo2_m1),
so M1 keeps lock-at-target with the $2.00 trail.
See [[feedback_m1_m3_candle_behaviour]].

This is a REAL BET, unlike locking at the target: it hands back $1 on
every trade that does not run, where lock-at-target costs only a few
cents of slippage. It is justified by the run rate, and if that falls
the trade-off reverses.

    python scripts/set_m3_runner_lock.py
    python scripts/set_m3_runner_lock.py --write

demo2 stays untouched as the control. Restart demo1_m3 afterwards.
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from bot.config import PROJECT_ROOT, load_config

TARGET = "demo1_m3"
UNTOUCHED = ("demo1_m1", "demo2_m1", "demo2_m3")
LOCK_BELOW = 1.00
TRAIL = 0.50

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--write", action="store_true")
args = p.parse_args()

path = PROJECT_ROOT / f"config/settings.{TARGET}.yaml"
with open(path, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

tp = float(raw["take_profit_usd"])
print(f"{TARGET}: take_profit_usd ${tp:.2f}")
print(f"  tp_runner_lock_below_usd  {raw.get('tp_runner_lock_below_usd', 0.0)} -> {LOCK_BELOW}"
      f"   (stop will lock at +${tp - LOCK_BELOW:.2f}, not +${tp:.2f})")
print(f"  tp_runner_trail_usd       {raw.get('tp_runner_trail_usd')} -> {TRAIL}")

if not args.write:
    print("\n(dry run -- nothing written. Re-run with --write.)")
    raise SystemExit(0)

raw["tp_runner_lock_below_usd"] = LOCK_BELOW
raw["tp_runner_trail_usd"] = TRAIL
with open(path, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

config = load_config(TARGET)
assert config.tp_runner_lock_below_usd == LOCK_BELOW, "lock_below did not take effect!"
assert config.tp_runner_trail_usd == TRAIL, "trail did not take effect!"
assert config.tp_runner_lock_below_usd < config.take_profit_usd, (
    "the lock must stay ABOVE break-even -- reaching the target has to remain a guaranteed win"
)
print(f"\nload_config('{TARGET}'): TP ${config.take_profit_usd:.2f}, "
      f"locks at +${config.take_profit_usd - config.tp_runner_lock_below_usd:.2f}, "
      f"trail ${config.tp_runner_trail_usd:.2f}")

print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.tp_runner_lock_below_usd == 0.0, (
        f"{account} has lock_below={other.tp_runner_lock_below_usd}, expected 0.0 -- "
        f"this change is demo1_m3 ONLY"
    )
    print(f"  Confirmed unchanged: {account} lock_below={other.tp_runner_lock_below_usd}, "
          f"trail={other.tp_runner_trail_usd}")

print("\nRestart demo1_m3, then: python scripts/verify_tp_runner_live.py")
