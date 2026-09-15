"""Change an account's trading symbol, safely.

User, 2026-09-16: live2 moved to a tighter-spread account where gold is
named XAUUSDp rather than XAUUSD.

The symbol is an IDENTITY field, so clone_strategy deliberately never
touches it -- which leaves hand-editing a real-money YAML as the only
route, and that has already produced an unloadable config once in this
project. This writes through the parser, verifies the result LOADS, and
reverts exactly if it does not.

It also does the thing a text edit cannot: connects and checks the symbol
EXISTS on that account before writing it, then prints the contract specs.
A typo'd symbol in a live config is a bot that cannot trade; a symbol with
a DIFFERENT CONTRACT SIZE is worse -- every dollar figure in the strategy
silently changes meaning, because a "$6 target" is six dollars of price
against 100 ounces per lot.

    python scripts/set_symbol.py --accounts live2_m3,live2_m5 --symbol XAUUSDp
    python scripts/set_symbol.py --accounts live2_m3,live2_m5 --symbol XAUUSDp --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys

import yaml

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a.strip()) for a in args.accounts.split(",")]
    changed = 0

    for account in accounts:
        path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        print("=" * 78)
        print(f"{account}   ->   {args.symbol}")
        print("=" * 78)
        if not path.is_file():
            print(f"  {path.name} not found — run this on the trading server.")
            continue

        doc = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        before = doc.get("symbol")
        if before == args.symbol:
            print(f"  already {args.symbol} — nothing to do")
            continue

        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            info = mt5.symbol_info(args.symbol)
            account_info = connector.account_info()
        finally:
            connector.disconnect()

        if info is None:
            print(f"  REFUSING: {args.symbol} does not exist on login "
                  f"{account_info.login}. A symbol the account cannot see is a bot")
            print("  that cannot trade. Run preflight_live.py to list what it does offer.")
            continue

        print(f"  login {account_info.login}   symbol found")
        print(f"    contract size   {info.trade_contract_size}")
        print(f"    digits / point  {info.digits} / {info.point}")
        print(f"    volume min/step {info.volume_min} / {info.volume_step}")
        print(f"    spread now      {(info.ask - info.bid):.2f}")
        if float(info.trade_contract_size) != 100.0:
            print("    *** CONTRACT SIZE IS NOT 100 OUNCES PER LOT. ***")
            print("    Every dollar figure in this strategy assumes 100. A $6 target")
            print("    would no longer be $6 of risk. Do NOT trade this until understood.")

        print(f"\n  symbol   {before}  ->  {args.symbol}")
        if not args.apply:
            print("  DRY RUN — nothing written. Re-run with --apply.")
            continue

        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, backup)
        doc["symbol"] = args.symbol
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
        try:
            reloaded = load_config(account)
        except Exception as exc:  # noqa: BLE001 - revert on ANY load failure
            shutil.copyfile(backup, path)
            backup.unlink(missing_ok=True)
            print(f"  REVERTED — the new config will not load:\n    {type(exc).__name__}: {exc}")
            print(f"  settings.{account}.yaml is back as it was.")
            continue
        backup.unlink(missing_ok=True)
        print(f"  written, parsed and LOADED OK: {path.name}  (symbol {reloaded.symbol})")
        print("  Restart the bot to load it — a running process holds the old symbol.")
        changed += 1

    if changed:
        print(f"\n{changed} config(s) changed. Now:")
        print("  python scripts/preflight_live.py --accounts " + args.accounts)
        print("  python scripts/verify_live2.py --allow stop_loss_usd")


if __name__ == "__main__":
    main()
