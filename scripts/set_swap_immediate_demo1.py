"""demo1_m1 + demo1_m3: swap on the FIRST opposing candle -- no debounce,
no ADX gate. The TP-runner is kept.

User decision 2026-09-08. The reason is the largest single defect found
in this project: the 2-candle debounce plus ADX(14) >= 25 blocked 100%
of reversals -- 0 swaps in 209 real trades. The swap is the engine's main
way of cutting a losing trade early, and it has never once fired. That
is why demo1 rides essentially every loss to a full stop (demo1_m3: 34
of 34 losses were full stops) while demo2, which has no gate, swaps out
of most of its (8 of 50). See [[project_demo1_swap_never_fires]].

The TP-runner is deliberately preserved: it lives only in this engine,
so switching demo1 to demo2's engine would delete it.

WHAT ELSE GOES WITH IT: the pending-reversal stop-tightening stops
happening. That rule only ever existed to protect the position DURING
the 2-candle wait, and there is no wait any more. On demo1_m3 it was
halving the stop to $3.50; that no longer occurs.

AN HONEST CAVEAT, because the case is not one-sided: over 2026-08-25..
09-07 demo1 OUTPERFORMED demo2 (+$486 vs +$109) with the gate blocking
everything, and the loss anatomy showed demo2's working swap does NOT
reduce total losses -- demo1_m3 lost $2,632 across 34 trades, demo2_m3
$2,521 across 50. The swap splits the same loss into more, smaller
pieces rather than shrinking it. So this is a change to a rule that was
demonstrably dead, not a change with proven upside.

    python scripts/set_swap_immediate_demo1.py
    python scripts/set_swap_immediate_demo1.py --write

demo2 stays the control. Restart both demo1 legs afterwards.
"""
import argparse
import sys

import yaml

sys.path.insert(0, ".")
from bot.config import PROJECT_ROOT, load_config

TARGETS = ("demo1_m1", "demo1_m3")
UNTOUCHED = ("demo2_m1", "demo2_m3")

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--write", action="store_true")
args = p.parse_args()

for account in TARGETS:
    path = PROJECT_ROOT / f"config/settings.{account}.yaml"
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f)
    print(f"  {account}: swap_immediate {raw.get('swap_immediate', False)} -> True")
    if not args.write:
        continue
    raw["swap_immediate"] = True
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

if not args.write:
    print("\n(dry run -- nothing written. Re-run with --write.)")
    raise SystemExit(0)

print()
for account in TARGETS:
    c = load_config(account)
    assert c.swap_immediate is True, f"{account}: swap_immediate did not take effect!"
    assert c.tp_runner_trail_usd is not None, (
        f"{account}: the TP-runner must survive this change -- it is the reason we did not "
        f"just switch to demo2's engine"
    )
    print(f"  {account}: swap_immediate={c.swap_immediate}, "
          f"TP-runner trail ${c.tp_runner_trail_usd:.2f} (kept), "
          f"stop ${c.stop_loss_usd:.2f}, TP ${c.take_profit_usd:.2f}")

print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.swap_immediate is False, (
        f"{account} has swap_immediate={other.swap_immediate}, expected False -- demo2 is the control"
    )
    print(f"  Confirmed unchanged: {account} swap_immediate={other.swap_immediate}")

print("\nRestart BOTH demo1 legs, then: python scripts/verify_tp_runner_live.py")
