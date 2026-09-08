"""demo1_m3: widen the TP-runner's arm window from $0.20 to $1.00.

The runner has to remove the broker take-profit BEFORE price reaches it
-- a limit order sitting at the broker fills in microseconds and a
1-second polling loop cannot beat it. `tp_runner_arm_before_usd` is how
far ahead of the target that removal starts.

Real evidence (decisions.jsonl, 2026-09-07..08), every tp_runner_armed
event so far:

    demo1_m1  $4.84 $4.85 $4.84 $4.87  (target $5.00)  4/4 removed
    demo1_m3  $5.92 $5.93 $6.02        (target $6.00)  removed
    demo1_m3  $6.12 $6.16              (target $6.00)  REMOVAL FAILED

Every failure fired PAST the target -- the take-profit had already
filled. M1 catches it every time; M3 misses 2 in 5, and that is why
demo1_m3 has produced zero runners despite the rule being live.

It is the candle difference, not a coding error: M1's window is
$4.80-$5.00 and its candles crawl through it, while M3's is $5.80-$6.00
with candles 1.7x larger -- price cleared $0.32 in one second and jumped
the whole window. See [[feedback_m1_m3_candle_behaviour]].

$1.00 gives roughly five times the room, so the bot has several polls to
act. M1 is NOT changed: 4 of 4 is working, and widening it there would
give up the guaranteed take-profit fill earlier for no benefit.

THE COST: between +$5.00 and the lock, demo1_m3 carries no broker
take-profit. Its breakeven arms at +$5.50 and the software stop is
running, so the exposure is a bot death inside that window. A failed
removal is harmless by comparison -- the trade simply closes at target
as it always did -- so this trades a small new risk for a rule that
actually fires.

    python scripts/widen_m3_runner_arm.py
    python scripts/widen_m3_runner_arm.py --write
"""
import argparse
import sys

import yaml

sys.path.insert(0, ".")
from bot.config import PROJECT_ROOT, load_config

TARGET = "demo1_m3"
NEW_ARM = 1.00

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--write", action="store_true")
args = p.parse_args()

path = PROJECT_ROOT / f"config/settings.{TARGET}.yaml"
with open(path, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

tp = float(raw["take_profit_usd"])
old = raw.get("tp_runner_arm_before_usd", 0.20)
print(f"{TARGET}: take_profit_usd ${tp:.2f}")
print(f"  tp_runner_arm_before_usd  {old} -> {NEW_ARM}")
print(f"  removal now starts at +${tp - NEW_ARM:.2f} instead of +${tp - float(old):.2f}")

if not args.write:
    print("\n(dry run -- nothing written. Re-run with --write.)")
    raise SystemExit(0)

raw["tp_runner_arm_before_usd"] = NEW_ARM
with open(path, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

c = load_config(TARGET)
assert c.tp_runner_arm_before_usd == NEW_ARM, "arm_before did not take effect!"
assert c.tp_runner_arm_before_usd < c.take_profit_usd, "the arm point must sit below the target"
print(f"\nload_config('{TARGET}'): arms at +${c.take_profit_usd - c.tp_runner_arm_before_usd:.2f}, "
      f"locks at +${c.take_profit_usd - c.tp_runner_lock_below_usd:.2f}, "
      f"trails ${c.tp_runner_trail_usd:.2f}, breakeven ${c.breakeven_trigger_usd:.2f}")

m1 = load_config("demo1_m1")
assert m1.tp_runner_arm_before_usd == 0.20, "demo1_m1 must stay at $0.20 -- it is working 4/4"
print(f"  Confirmed unchanged: demo1_m1 arm_before ${m1.tp_runner_arm_before_usd:.2f}")

print("\nRestart demo1_m3, then watch for 'broker take-profit removed' rather than")
print("'REMOVAL FAILED' in the next tp_runner_armed lines.")
