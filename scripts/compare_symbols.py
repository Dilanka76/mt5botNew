"""Compare several symbols on ONE account, at one moment.

Why this exists (2026-09-07): the live account offers XAUUSD while demo
has been trading XAUUSDp -- and the demo server turns out to offer BOTH.
A broker suffix like that usually marks a different pricing model (often
tighter spread plus commission, versus wider spread and none), not a
different market. If XAUUSDp is cheaper to trade than XAUUSD, then
demo2's three weeks of results were earned on a cheaper instrument than
live2 will trade, and the live results will be systematically worse by
roughly the spread difference times the number of trades.

Reading both symbols from the SAME terminal in the SAME call is what
makes the comparison meaningful: gold spreads widen and narrow through
the day, so two readings taken minutes apart on different terminals
prove nothing.

    python scripts/compare_symbols.py --account demo2_m1 --symbols XAUUSDp,XAUUSD,XAUUSD-PERP
    python scripts/compare_symbols.py --account live2_m1 --symbols XAUUSD,XAUUSD-PERP

Run it while the market is OPEN -- a closed market reports a stale or
zero spread. Read-only: places no orders.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True)
    p.add_argument("--symbols", required=True, help="comma-separated")
    p.add_argument("--lots", type=float, default=0.12, help="lot size for the $ cost column")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    account = validate_account_name(args.account)
    config = load_config(account)
    names = [s.strip() for s in args.symbols.split(",") if s.strip()]

    connector = MT5Connector(config.mt5)
    connector.connect()
    rows = []
    try:
        import MetaTrader5 as mt5
        for name in names:
            try:
                connector.ensure_symbol(name)
            except Exception:  # noqa: BLE001 - a missing symbol is a result, not a crash
                pass
            info = mt5.symbol_info(name)
            tick = mt5.symbol_info_tick(name)
            rows.append((name, info, tick))
    finally:
        connector.disconnect()

    print(f"{account} ({config.mt5.server or 'server from .env'}) — all read in one call\n")
    print(f"{'symbol':<16}{'contract':<10}{'spread $':<11}{'cost/trade':<12}"
          f"{'swap long':<12}{'swap short':<12}commission-style")
    for name, info, tick in rows:
        if info is None:
            print(f"{name:<16}NOT AVAILABLE on this account")
            continue
        spread = (tick.ask - tick.bid) if tick else float("nan")
        cost = spread * args.lots * info.trade_contract_size
        print(f"{name:<16}{info.trade_contract_size:<10.0f}{spread:<11.3f}"
              f"${cost:<11.2f}{info.swap_long:<12.3f}{info.swap_short:<12.3f}"
              f"{getattr(info, 'trade_mode', '?')}")

    print()
    print(f"cost/trade = spread x {args.lots} lots x contract size — what you pay to open,")
    print("before any commission the broker charges separately. Compare it against a")
    print(f"$5 take-profit, which at {args.lots} lots is worth "
          f"${5 * args.lots * (rows[0][1].trade_contract_size if rows and rows[0][1] else 100):.2f}.")
    print()
    print("If two symbols show very different spreads here, they are different")
    print("PRODUCTS, not different markets — and results measured on one do not")
    print("transfer to the other unchanged.")


if __name__ == "__main__":
    main()
