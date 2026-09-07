"""Pre-flight for live2, before real money trades.

Answers one question: is live2 EXACTLY demo2, apart from the execution
block that has to differ?

Compares the two configs field by field and reports every difference, so
a typo or a stale value cannot quietly change the strategy on the
real-money account. Then checks the things that only matter when it is
real money: the demo guard is off deliberately, the magic numbers cannot
collide with another account, and the terminal it connects to is the
account you think it is.

That last one is not theoretical here. On 2026-08-27 multi-terminal
cross-talk let a bot read the WRONG account's balance and mis-size a
real trade (see project_live_trading_safeguards). So this prints the
login, server and balance it actually reached and asks you to confirm
them by eye.

    python scripts/verify_live2.py

Read-only: connects to MT5 to read account/symbol info. Places no orders.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, discover_configured_accounts, load_config
from bot.mt5_connector import MT5Connector

PAIRS = [("demo2_m1", "live2_m1"), ("demo2_m3", "live2_m3")]
# The execution block is ALLOWED to differ -- everything else is not.
EXPECTED_DIFFS = {"magic_number", "sibling_magic_numbers", "require_demo_account",
                  "mode", "order_comment",
                  # The symbol NAME is allowed to differ -- the live account
                  # carries XAUUSD where demo carries XAUUSDp, and the suffix
                  # is account-specific. What must NOT differ is the CONTRACT
                  # behind it, which is compared field by field below
                  # (contract size, digits, point, volume steps, tick
                  # value/size). A matching name with a different contract
                  # would be far more dangerous than a differing name.
                  "symbol"}


def raw_config(account: str) -> dict | None:
    path = PROJECT_ROOT / f"config/settings.{account}.yaml"
    if not path.is_file():
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def main() -> None:
    problems: list[str] = []

    for source, target in PAIRS:
        print("=" * 78)
        print(f"{target}  vs  {source}")
        print("=" * 78)

        env_path = PROJECT_ROOT / f".env.{target}"
        if not env_path.is_file():
            print(f"  .env.{target}: MISSING <-- create it before anything else")
            problems.append(f"{target}: no .env file")
            print()
            continue
        print(f"  .env.{target}: present")

        src_raw, tgt_raw = raw_config(source), raw_config(target)
        if tgt_raw is None:
            print(f"  config/settings.{target}.yaml: MISSING <-- run create_live2_config.py --write")
            problems.append(f"{target}: no config file")
            print()
            continue

        src_flat, tgt_flat = flatten(src_raw), flatten(tgt_raw)
        keys = sorted(set(src_flat) | set(tgt_flat))
        unexpected = []
        for key in keys:
            a, b = src_flat.get(key, "<missing>"), tgt_flat.get(key, "<missing>")
            if a == b:
                continue
            # A key absent on one side and explicitly null on the other is
            # the SAME setting -- both mean "off". Flagging that as a
            # difference is noise, and noise in a pre-flight check is how
            # a real difference gets skimmed past.
            # Membership test, not a set: some values here are lists
            # (sibling_magic_numbers), which are unhashable.
            if a in ("<missing>", None) and b in ("<missing>", None):
                continue
            leaf = key.split(".")[-1]
            if leaf in EXPECTED_DIFFS:
                print(f"  expected diff  {key}: {a} -> {b}")
            else:
                print(f"  UNEXPECTED     {key}: {source}={a}  {target}={b}  <-- must match")
                unexpected.append(key)
        if unexpected:
            problems.append(f"{target}: {len(unexpected)} field(s) differ from {source}")
        else:
            print(f"  Every strategy field matches {source} exactly.")

        config = load_config(target)
        print(f"  mode={config.execution.mode}  require_demo_account={config.execution.require_demo_account}  "
              f"magic={config.execution.magic_number}")
        if config.execution.mode != "live_execute":
            print("    <-- mode must be live_execute for a real account (it also excludes live2")
            print("        from the app's stop-all/start-all, as live1 is)")
            problems.append(f"{target}: mode is not live_execute")
        if config.execution.require_demo_account:
            print("    <-- require_demo_account is TRUE: the bot will refuse to trade here")
            problems.append(f"{target}: require_demo_account still true")
        if config.tp_runner_trail_usd is not None:
            print(f"    <-- tp_runner_trail_usd={config.tp_runner_trail_usd}: demo2 does NOT have this")
            problems.append(f"{target}: has the TP-runner, demo2 does not")
        print(f"  daily_loss_limit_usd={config.daily_loss_limit_usd}"
              f"{'  (OFF -- no cap on a bad day)' if not config.daily_loss_limit_usd else ''}")

        # magic collision across every configured account
        for other in discover_configured_accounts():
            if other == target:
                continue
            other_raw = raw_config(other)
            if other_raw and (other_raw.get("execution") or {}).get("magic_number") == config.execution.magic_number:
                print(f"  MAGIC COLLISION with {other} <-- each bot would manage the other's trades")
                problems.append(f"{target}: magic collides with {other}")

        # what the terminal actually is
        try:
            connector = MT5Connector(config.mt5)
            connector.connect()
            try:
                import MetaTrader5 as mt5
                info = mt5.account_info()
                symbol = mt5.symbol_info(config.symbol)
            finally:
                connector.disconnect()
            if info is None:
                print("  MT5: connected but account_info() returned None")
                problems.append(f"{target}: no account_info")
            else:
                print(f"  MT5 REACHED: login={info.login}  server={info.server}  "
                      f"balance={info.balance:.2f} {info.currency}  demo={'YES' if info.trade_mode == 0 else 'NO'}")
                print(f"               CONFIRM BY EYE that this is your live2 account.")
                if info.trade_mode == 0:
                    print("    <-- this terminal is on a DEMO account, not a live one")
                    problems.append(f"{target}: connected terminal is a demo account")
            if symbol is not None:
                print(f"  symbol {config.symbol}: found")
                # "Exactly like demo2" has to include the INSTRUMENT. Position
                # sizing, every stop/target in dollars and every figure in this
                # project's research assume $100 per lot per $1 of price. A
                # different contract size on the live symbol would silently
                # rescale all of it -- a $10 stop would not be $10 of risk.
                src_config = load_config(source)
                src_connector = MT5Connector(src_config.mt5)
                src_connector.connect()
                try:
                    src_symbol = mt5.symbol_info(src_config.symbol)
                finally:
                    src_connector.disconnect()
                if src_symbol is None:
                    print(f"    (could not read {source}'s {src_config.symbol} to compare specs)")
                else:
                    fields = [
                        ("contract size", "trade_contract_size"),
                        ("digits", "digits"),
                        ("point", "point"),
                        ("volume min", "volume_min"),
                        ("volume step", "volume_step"),
                        ("tick value", "trade_tick_value"),
                        ("tick size", "trade_tick_size"),
                    ]
                    mismatched = []
                    for label, attr in fields:
                        a, b = getattr(src_symbol, attr), getattr(symbol, attr)
                        if a != b:
                            mismatched.append(f"{label} {a} -> {b}")
                    if mismatched:
                        print(f"    CONTRACT DIFFERS from {source}'s {src_config.symbol}:")
                        for m in mismatched:
                            print(f"      {m}")
                        print("      Every dollar figure (stops, targets, lot tiers, all the")
                        print("      research) assumes demo2's contract. Do NOT trade until")
                        print("      this is understood.")
                        problems.append(f"{target}: contract specs differ from {source}")
                    else:
                        print(f"    contract specs identical to {source}'s {src_config.symbol}")
            else:
                print(f"  symbol {config.symbol}: NOT FOUND on this account")
                problems.append(f"{target}: symbol {config.symbol} not available")
                # Live accounts routinely use a different suffix from demo
                # (XAUUSD, XAUUSD.r, XAUUSDm ...). Guessing is how you end
                # up trading the wrong instrument, so list the real ones.
                try:
                    connector.connect()
                    try:
                        candidates = [s.name for s in (mt5.symbols_get() or []) if "XAU" in s.name.upper()]
                    finally:
                        connector.disconnect()
                    if candidates:
                        print("    gold symbols this account DOES offer:")
                        for name in sorted(candidates):
                            print(f"      {name}")
                        print("    Put the right one in BOTH live2 configs' `symbol:` field.")
                    else:
                        print("    No XAU symbol found at all -- the account may not have gold")
                        print("    enabled, or Market Watch may need 'Show All' in the terminal.")
                except Exception as exc:  # noqa: BLE001
                    print(f"    (could not list symbols: {exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"  MT5 CONNECTION FAILED: {exc}")
            problems.append(f"{target}: cannot connect ({exc})")
        print()

    print("=" * 78)
    if problems:
        print(f"{len(problems)} problem(s) -- DO NOT go live until these are resolved:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("live2 mirrors demo2 exactly, connects to a live account, and has no")
        print("magic collision. Remaining by hand: Task Scheduler tasks, and deciding")
        print("whether to set daily_loss_limit_usd before it trades.")


if __name__ == "__main__":
    main()
