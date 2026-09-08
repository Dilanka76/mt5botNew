"""demo1_m3: apply the two changes decided 2026-09-08, in one place.

  1. stop_loss_usd   $10.00 -> $7.00
  2. TP-runner: lock $1 BELOW the target and trail $0.50 behind

WHY THE STOP (point 2 of the four structural fixes): the six-month
backtest showed the strategy needs a 54.1% win rate and delivers 52.6%,
because the average LOSS ($30.46) is bigger than the average WIN
($25.83). Sweeping the stop with lot size held fixed
(scripts/stop_loss_sweep_analysis.py --balance 100000, demo2_m3,
2026-03-01..09-01, 2058 trades) shows the $10 stop is the cause:

    $10   -$32      <- current, break-even at best
     $8  +$917
     $7 +$1,485     <- this
     $6  +$962
     $5 +$2,175

$5 scored highest but is NOT chosen: it sits at the edge of the tested
range with no idea what lies below it, and it changes what the strategy
IS -- stops go from 331 to 932 while swaps fall from 648 to 217, so the
stop replaces the swap as the main exit. $7 is an interior point whose
neighbours are both positive, and on M3 (candle range ~$1-3) it keeps a
real margin over normal noise while still cutting the average loss from
$79.65 to $68.46.

WHY THE RUNNER LOCK (point 1): locking exactly at the target means the
dip that normally follows it stops the trade out at once -- six of the
first seven real locks did exactly that. Locking at $5.00 lets the trade
survive the dip. Combined across both M3 accounts: +$1,211 versus +$984.
The tight $0.50 trail belongs with it -- the lock now supplies the
breathing room, so the trail's only job is to keep what a run earned.

The two changes attack the arithmetic from opposite ends: the runner
raises the average win, the stop lowers the average loss.

M1 gets NEITHER. Its $5 target cannot afford a $1 give-back, and a
tighter trail there was mixed (+$71 demo1_m1, -$21 demo2_m1). See
[[feedback_m1_m3_candle_behaviour]]. demo2 stays the untouched control.

    python scripts/update_demo1_m3_strategy.py
    python scripts/update_demo1_m3_strategy.py --write
"""
import argparse
import sys

import yaml

sys.path.insert(0, ".")
from bot.config import PROJECT_ROOT, load_config

TARGET = "demo1_m3"
UNTOUCHED = ("demo1_m1", "demo2_m1", "demo2_m3")
NEW = {"stop_loss_usd": 7.0, "tp_runner_lock_below_usd": 1.0, "tp_runner_trail_usd": 0.5}

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--write", action="store_true")
args = p.parse_args()

path = PROJECT_ROOT / f"config/settings.{TARGET}.yaml"
with open(path, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

print(f"{TARGET}:")
for key, value in NEW.items():
    print(f"  {key:<26} {raw.get(key)} -> {value}")

if not args.write:
    print("\n(dry run -- nothing written. Re-run with --write.)")
    raise SystemExit(0)

raw.update(NEW)
with open(path, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

c = load_config(TARGET)
for key, value in NEW.items():
    assert getattr(c, key) == value, f"{key} did not take effect!"
assert c.tp_runner_lock_below_usd < c.take_profit_usd, (
    "the runner lock must stay above break-even -- reaching the target has to remain a win"
)
assert c.breakeven_trigger_usd is not None and c.breakeven_trigger_usd < c.take_profit_usd, (
    "the breakeven trigger must sit below the target -- it covers the window before the runner locks"
)

lock_at = c.take_profit_usd - c.tp_runner_lock_below_usd
print(f"\nThe full demo1_m3 ladder, as it will now run:")
print(f"    profit $0.00 -> ${c.breakeven_trigger_usd:.2f}   stop at -${c.stop_loss_usd:.2f}")
print(f"    ${c.breakeven_trigger_usd:.2f} -> ${c.take_profit_usd:.2f}          stop at entry + ${c.breakeven_lock_usd:.2f}")
print(f"    ${c.take_profit_usd:.2f} and up            trade STAYS OPEN, stop locks at +${lock_at:.2f},")
print(f"                              then trails ${c.tp_runner_trail_usd:.2f} behind the best price")
print(f"    an opposite confirmed cross still closes the trade at any point")

print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.tp_runner_lock_below_usd == 0.0, f"{account} lock_below changed -- must stay 0.0!"
    print(f"  Confirmed unchanged: {account} stop ${other.stop_loss_usd:.2f}, "
          f"lock_below {other.tp_runner_lock_below_usd}, trail {other.tp_runner_trail_usd}")

print(f"\nRestart demo1_m3, then: python scripts/verify_tp_runner_live.py")
