"""demo1_m3: move the breakeven trigger $5.50 -> $5.00, to match where
the TP-runner now removes the broker take-profit.

Widening tp_runner_arm_before_usd to $1.00 (commit 739d8ec) made the
take-profit come off at +$5.00, but the breakeven still armed at +$5.50.
That left a window nothing covered:

    profit $5.00 - $5.50   no take-profit, stop still at -$7.00

A trade could reach +$5.00, lose its take-profit, reverse, and run to
-$7.00 -- a $13 swing where it would previously have banked $6. Before
the widening this could not happen: the breakeven ($5.50) always armed
BEFORE the take-profit came off ($5.80).

Moving the trigger to $5.00 means the breakeven stop is in place at the
exact moment the take-profit is removed, so the trade is never exposed.

Supported by the sweep already run (scripts/simulate_breakeven_rule.py,
demo1_m3, 2026-08-25 onward): a $5.00 trigger passed BOTH walk-forward
halves (+$5.88 / +$90.72), as did $5.50 (+$71.16 / +$22.08). Both work
on this account; $5.00 is the one that fits the runner.

demo1_m1 is untouched -- its arm window is $0.20 and its breakeven at
$4.50 still arms first, so it has no gap.

    python scripts/align_m3_breakeven.py
    python scripts/align_m3_breakeven.py --write
"""
import argparse
import sys

import yaml

sys.path.insert(0, ".")
from bot.config import PROJECT_ROOT, load_config

TARGET = "demo1_m3"
NEW_TRIGGER = 5.00

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--write", action="store_true")
args = p.parse_args()

path = PROJECT_ROOT / f"config/settings.{TARGET}.yaml"
with open(path, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

print(f"{TARGET}: breakeven_trigger_usd {raw.get('breakeven_trigger_usd')} -> {NEW_TRIGGER}")

if not args.write:
    print("\n(dry run -- nothing written. Re-run with --write.)")
    raise SystemExit(0)

raw["breakeven_trigger_usd"] = NEW_TRIGGER
with open(path, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

c = load_config(TARGET)
arm_at = c.take_profit_usd - c.tp_runner_arm_before_usd
assert c.breakeven_trigger_usd == NEW_TRIGGER, "trigger did not take effect!"
assert c.breakeven_trigger_usd <= arm_at, (
    f"the breakeven (${c.breakeven_trigger_usd:.2f}) must arm at or before the take-profit is "
    f"removed (${arm_at:.2f}), or the trade is exposed in between"
)

print(f"\nThe full demo1_m3 ladder:")
print(f"    $0.00 -> $5.00   stop at -${c.stop_loss_usd:.2f}")
print(f"    $5.00            take-profit removed AND stop -> entry + ${c.breakeven_lock_usd:.2f} "
      f"(same moment, no gap)")
print(f"    $6.00 and up     stop locks at +${c.take_profit_usd - c.tp_runner_lock_below_usd:.2f}, "
      f"trails ${c.tp_runner_trail_usd:.2f}")

m1 = load_config("demo1_m1")
print(f"\n  Confirmed unchanged: demo1_m1 breakeven ${m1.breakeven_trigger_usd:.2f}, "
      f"arm_before ${m1.tp_runner_arm_before_usd:.2f} "
      f"(arms at +${m1.take_profit_usd - m1.tp_runner_arm_before_usd:.2f} — breakeven still first, no gap)")
print("\nRestart demo1_m3.")
