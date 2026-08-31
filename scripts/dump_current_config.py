"""One-off: prints the real, current live config for all four accounts
(stop/take-profit, breakeven trigger, gap threshold, sessions) via the real
load_config() -- used to populate reports/content/strategy_overview.json
with real numbers instead of guessing from the (stale, out-of-sync) local
config/*.yaml copies in the mt5tradingbot Mac clone.

Run this ON THE SERVER, in the mt5bot repo directory.
"""
import sys

sys.path.insert(0, ".")
from bot.config import load_config

ACCOUNTS = ("demo1_m1", "demo1_m3", "demo2_m1", "demo2_m3")

for account in ACCOUNTS:
    config = load_config(account)
    print(f"=== {account} ===")
    print(f"strategy_variant={config.strategy_variant}")
    print(f"timeframe={config.timeframe}")
    print(f"stop_loss_usd={config.stop_loss_usd}")
    print(f"take_profit_usd={config.take_profit_usd}")
    print(f"breakeven_trigger_usd={config.breakeven_trigger_usd}")
    print(f"gap_threshold_usd={config.gap_threshold_usd}")
    sessions = config.sessions.get(config.strategy_variant, [])
    print(f"sessions={[(w.start, w.end) for w in sessions]}")
    print()
