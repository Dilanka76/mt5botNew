"""Sets the trading symbol on both live2 legs.

The live account does not carry demo's XAUUSDp -- it offers XAUUSD. The
suffix is broker/account-specific and has changed before in this project,
so the name is set here explicitly rather than derived or guessed.

Refuses a symbol the live terminal does not actually offer, and refuses
XAUUSD-PERP by name: a perpetual has different contract specs and
financing from spot gold, and picking it by accident would silently
change what every dollar figure in this project means.

    python scripts/set_live2_symbol.py --symbol XAUUSD
    python scripts/set_live2_symbol.py --symbol XAUUSD --write

Shows by default; changes nothing without --write.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, load_config
from bot.mt5_connector import MT5Connector

TARGETS = ("live2_m1", "live2_m3")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", required=True)
    p.add_argument("--write", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    symbol = args.symbol.strip()

    if symbol.upper().endswith("-PERP"):
        raise SystemExit(
            "REFUSING: a -PERP contract is a perpetual, not spot gold. Its contract size and "
            "financing differ, so every dollar figure in this project would quietly mean "
            "something else. Use the spot symbol."
        )

    # Confirm the live terminal really offers it before writing anything.
    config = load_config(TARGETS[0])
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        import MetaTrader5 as mt5
        connector.ensure_symbol(symbol)
        info = mt5.symbol_info(symbol)
    finally:
        connector.disconnect()

    if info is None:
        raise SystemExit(f"REFUSING: the live terminal does not offer '{symbol}'.")

    print(f"{symbol} on the LIVE account:")
    print(f"  contract size : {info.trade_contract_size}")
    print(f"  digits/point  : {info.digits} / {info.point}")
    print(f"  volume min/step: {info.volume_min} / {info.volume_step}")
    print(f"  tick value/size: {info.trade_tick_value} / {info.trade_tick_size}")
    print(f"  spread        : {info.spread} points")
    print()

    for account in TARGETS:
        path = PROJECT_ROOT / f"config/settings.{account}.yaml"
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = yaml.safe_load(f)
        old = raw.get("symbol")
        print(f"  {account}: symbol {old} -> {symbol}")
        if not args.write:
            continue
        raw["symbol"] = symbol
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

    if not args.write:
        print("\n(dry run -- nothing written. Re-run with --write.)")
    else:
        print("\nWritten. Now re-run:  python scripts/verify_live2.py")
        print("It compares the LIVE contract specs against demo2's, because position")
        print("sizing assumes $100 per lot per $1 of price -- if the contract differs,")
        print("every risk figure in this project means something else on live2.")


if __name__ == "__main__":
    main()
