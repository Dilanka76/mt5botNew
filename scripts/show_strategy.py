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
import ast
import pathlib
import sys

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, discover_configured_accounts, load_config, validate_account_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3")
    return p.parse_args()


def _engine_module_for(variant: str) -> pathlib.Path | None:
    """Resolve strategy_variant -> the engine module main.py would run.

    Parses main.py's STRATEGY_ENGINES with ast rather than importing it,
    because importing main pulls in MetaTrader5, which has Windows-only
    wheels and cannot load on a dev Mac.
    """
    try:
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    class_name = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "STRATEGY_ENGINES" for t in node.targets
        ) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value == variant and isinstance(v, ast.Name):
                    class_name = v.id
    if class_name is None:
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if any(a.name == class_name for a in node.names):
                path = PROJECT_ROOT / (node.module.replace(".", "/") + ".py")
                return path if path.is_file() else None
    return None


def _fields_read_by(module_path: pathlib.Path | None) -> set[str] | None:
    """Config attribute names the engine actually reads in CODE.

    Uses ast so that a field merely NAMED in a docstring does not count --
    these docstrings discuss settings they no longer use, which is exactly
    the confusion this guards against.
    """
    if module_path is None:
        return None
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def note(field: str, reads: set[str] | None) -> str:
    """Flag settings the running engine ignores -- a configured value that
    no engine reads is dead, and reporting it as active is misleading."""
    if reads is None or field in reads:
        return ""
    return "   <-- IGNORED by this engine (dead setting)"


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
        reads = _fields_read_by(_engine_module_for(c.strategy_variant))

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
              f"(gap < this = enter immediately; gap >= this = wait for an EMA5 pullback)"
              f"{note('gap_threshold_usd', reads)}")
        print(f"    Entry filter       : {'ON' if c.entry_filter_enabled else 'OFF'} "
              f"(candle colour + tick volume)"
              f"{note('entry_filter_enabled', reads)}")
        early = c.early_entry_threshold_usd
        print(f"    Early entry        : {'OFF' if early is None else money(early)}"
              f"{note('early_entry_threshold_usd', reads)}")

        print("  EXIT")
        print(f"    Take profit        : {money(c.take_profit_usd)}  (broker-side)")
        print(f"    Stop loss          : {money(c.stop_loss_usd)}")
        trail = c.tp_runner_trail_usd
        if trail is None:
            print(f"    TP-runner          : OFF (trade closes at take profit)")
        else:
            lock_at = c.take_profit_usd - c.tp_runner_lock_below_usd
            print(f"    TP-runner          : ON — at the target the trade STAYS OPEN, stop locks at "
                  f"+${lock_at:.2f}, trails ${trail:.2f} behind"
                  f"{note('tp_runner_trail_usd', reads)}")
        print(f"    Daily loss limit   : "
              f"{'OFF' if not c.daily_loss_limit_usd else money(c.daily_loss_limit_usd)}"
              f"{note('daily_loss_limit_usd', reads)}")

        be_trig, be_lock = c.breakeven_trigger_usd, c.breakeven_lock_usd
        if be_trig is None:
            print(f"    Breakeven          : OFF")
        else:
            print(f"    Breakeven          : arms at {money(be_trig)} favourable, "
                  f"then stop moves to entry{'' if not be_lock else f' + {money(be_lock)} locked profit'}"
                  f"{note('breakeven_trigger_usd', reads)}")

        print("  REVERSAL / SWAP")
        # swap_immediate wins over BOTH the debounce and the ADX gate, so it
        # has to be tested first. Branching on swap_adx_filter alone printed
        # "Debounce: 2 candles / ADX gate: >= 25" for demo1_m1 and demo1_m3,
        # which have run swap_immediate=True since 2026-09-08 -- describing
        # the exact rules that were deliberately switched off, on the two
        # accounts whose config anyone is most likely to check.
        if getattr(c, "swap_immediate", False):
            print(f"    Swap gate          : NONE -- swap_immediate is ON")
            print(f"    Debounce           : off (fires on the FIRST opposing candle close)")
            print(f"    ADX gate           : off (bypassed entirely)"
                  + ("" if c.swap_adx_filter is None else
                     f"  [swap_adx_filter is set to ADX({c.swap_adx_filter.adx_period}) >= "
                     f"{c.swap_adx_filter.adx_threshold:.1f} but is NOT consulted]"))
        elif c.swap_adx_filter is None:
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
