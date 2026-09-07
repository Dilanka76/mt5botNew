"""Print the EXACT strategy each account is really running.

Everything comes from load_config() -- the same loader main.py uses --
so this reflects the live YAML on this machine, not documentation. The
engine docstrings in bot/strategy/ have drifted from reality more than
once (the dual_cross_confirmed_swap_adx docstring still describes a $15
stop, a $5 take-profit and a gap/EMA5 pullback rule, none of which match
the current configs), so read this, not them.

    python scripts/show_strategy.py
    python scripts/show_strategy.py --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3

Read-only: opens no MT5 connection and touches no trading state.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from bot.config import discover_configured_accounts, load_config, validate_account_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    return p.parse_args()


def money(v: float | None) -> str:
    return "not set" if v is None else f"${v:.2f}"


def main() -> None:
    args = parse_args()
    known = set(discover_configured_accounts())
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    for account in accounts:
        if account not in known:
            print(f"{account}: no config on this machine (configured here: {sorted(known)})\n")
            continue
        c = load_config(account)

        print("=" * 78)
        print(f"{account}   {c.symbol} {c.timeframe}   engine: {c.strategy_variant}")
        print("=" * 78)

        print("  ENTRY")
        # The entry cross is mid vs slow (ema13/ema21 columns), NOT fast/mid --
        # compute_emas() maps fast->ema5, reversal_slow->ema9, mid->ema13,
        # slow->ema21, and every engine tests ema13 > ema21. ema5 is the
        # pullback line for the gap rule; ema9 pairs with it for reversals.
        print(f"    EMA cross          : EMA{c.ema_periods.mid}/EMA{c.ema_periods.slow} "
              f"(the entry signal)")
        print(f"    Other EMAs         : EMA{c.ema_periods.fast} (pullback line for the gap rule), "
              f"EMA{c.ema_periods.reversal_slow} (reversal check)")
        print(f"    Confirmation       : cross must be confirmed at candle CLOSE")
        # gap < threshold -> immediate; gap >= threshold -> wait for an EMA5
        # touch. A threshold far above any real gap means "always immediate".
        print(f"    gap_threshold_usd  : {money(c.gap_threshold_usd)}  "
              f"(gap < this = enter immediately; gap >= this = wait for an EMA5 pullback)")
        print(f"    Entry filter       : {'ON' if c.entry_filter_enabled else 'OFF'} "
              f"(candle colour + tick volume)")
        early = c.early_entry_threshold_usd
        print(f"    Early entry        : {'OFF' if early is None else money(early)}")

        print("  EXIT")
        print(f"    Take profit        : {money(c.take_profit_usd)}  (broker-side)")
        print(f"    Stop loss          : {money(c.stop_loss_usd)}")
        be_trig, be_lock = c.breakeven_trigger_usd, c.breakeven_lock_usd
        if be_trig is None:
            print(f"    Breakeven          : OFF")
        else:
            print(f"    Breakeven          : arms at {money(be_trig)} favourable, "
                  f"then stop moves to entry{'' if not be_lock else f' + {money(be_lock)} locked profit'}")

        print("  REVERSAL / SWAP")
        if c.swap_adx_filter is None:
            print(f"    Swap gate          : none -- reversal fires as soon as it is confirmed")
        else:
            print(f"    Debounce           : 2 candles (arm on the first opposing close, "
                  f"fire only if the next candle still opposes)")
            print(f"    ADX gate           : ADX({c.swap_adx_filter.adx_period}) >= "
                  f"{c.swap_adx_filter.adx_threshold:.1f} at the confirming candle, else the swap is dropped")

        print("  SIZE / SCHEDULE")
        for tier in c.position_sizing:
            cap = "no upper bound" if tier.max_balance is None else f"up to ${tier.max_balance:,.2f}"
            print(f"    Lots               : {tier.lots}  ({cap})")
        windows = c.sessions.get(c.strategy_variant, [])
        if not windows:
            print(f"    Sessions           : none configured for {c.strategy_variant} (trades all day)")
        for w in windows:
            print(f"    Session (Colombo)  : {w.start} - {w.end}")

        print("  EXECUTION")
        print(f"    Mode               : {c.execution.mode}   magic={c.execution.magic_number}   "
              f"demo-only guard={c.execution.require_demo_account}")
        print()


if __name__ == "__main__":
    main()
