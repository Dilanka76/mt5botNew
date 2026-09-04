"""One-off: enables the entry-quality filter on demo1_m3 ONLY --
explicit user decision 2026-09-04 after reviewing real forward shadow
data.

The filter skips a confirmed EMA13/21 cross unless BOTH (a) the
confirming candle closed in the trade's own direction, and (b) its tick
volume is not in the bottom third of the trailing window.

Evidence (real forward shadow data, not backtest):
  demo1_m3: trades passing both checks won 82.4% (+$43.89/trade);
            trades either check would block won 8.3% (-$63.85/trade).
  demo2_m3: passing 76.9% (+$30.42/trade); blocked 23.5% (-$26.96/trade).
  The two checks overlap on only 8-12% of blocked trades, so both are
  required rather than either alone (both together were ~3x better per
  trade than either alone).

Scope is deliberately narrow:
  - demo1_m3 ONLY. demo2_m3 is the model for next week's live launch and
    must NOT diverge from what has three weeks of proven history.
  - NOT the M1 legs: the same checks made results WORSE there in the
    same forward data (a 1-minute candle's colour/volume is mostly
    noise). See bot/config.py's entry_filter_enabled comment.

Expect roughly 40% fewer trades on demo1_m3. Every skipped signal is
logged as `entry_filtered` with the specific reason, so the decision can
be audited against what would have happened.
"""
import sys

import yaml
from pathlib import Path

sys.path.insert(0, ".")
from bot.config import load_config

TARGET = "demo1_m3"
UNTOUCHED = ("demo1_m1", "demo2_m1", "demo2_m3")

p = Path(f"config/settings.{TARGET}.yaml")
with open(p, "r", encoding="utf-8-sig") as f:
    raw = yaml.safe_load(f)

old = raw.get("entry_filter_enabled")
raw["entry_filter_enabled"] = True

with open(p, "w", encoding="utf-8") as f:
    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
print(f"Updated {p}: entry_filter_enabled={old} -> True")

# Verify through the real loader before trusting it.
print()
config = load_config(TARGET)
print(f"load_config('{TARGET}'): entry_filter_enabled={config.entry_filter_enabled}, "
      f"strategy_variant={config.strategy_variant}, timeframe={config.timeframe}, "
      f"stop_loss_usd={config.stop_loss_usd}, take_profit_usd={config.take_profit_usd}")
assert config.entry_filter_enabled is True, "entry_filter_enabled did not take effect!"
assert config.timeframe == "M3", f"{TARGET} is not M3 -- this filter is M3-only!"

# Every other account must be completely unaffected.
print()
for account in UNTOUCHED:
    other = load_config(account)
    assert other.entry_filter_enabled is False, (
        f"{account}'s entry_filter_enabled is {other.entry_filter_enabled}, expected False -- "
        f"this change must touch demo1_m3 ONLY!"
    )
    print(f"Confirmed: {account} unaffected (entry_filter_enabled={other.entry_filter_enabled})")

print(f"\nConfirmed: entry filter enabled on {TARGET} only. "
      f"demo2_m3 (the live model) is untouched.")
