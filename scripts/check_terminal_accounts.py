"""Ground-truth check: connects directly to EACH terminal64.exe path in
turn (bypassing any .env/config file) and reports which account it's
actually logged into right now. Built 2026-08-27 to diagnose a suspected
cross-account mixup after demo2_m1/demo2_m3's .env files were found to
have a blank MT5_TERMINAL_PATH (main.py's mt5.initialize() then attaches
to whatever terminal MT5 resolves as default, which may not be the
intended one -- and since login/password ARE set, it then force-logs
whatever terminal it landed on into the wrong account).

    python scripts/check_terminal_accounts.py

Read-only -- only calls mt5.account_info(), never places or touches any
order. Cleanly disconnects between each terminal so one check can't leak
into the next.
"""
from __future__ import annotations

import MetaTrader5 as mt5

TERMINALS = {
    "demo1 (MT5-Demo1 T)": r"C:\MT5BOTSCRIPT\MT5-Demo1 T\terminal64.exe",
    "demo2 (MT5-Demo2 D)": r"C:\MT5BOTSCRIPT\MT5-Demo2 D\terminal64.exe",
    "live1 (MT5-Live1 T)": r"C:\MT5BOTSCRIPT\MT5-Live1 T\terminal64.exe",
}


def main() -> None:
    for label, path in TERMINALS.items():
        ok = mt5.initialize(path=path, timeout=10000)
        if not ok:
            print(f"{label}: FAILED to connect -- {mt5.last_error()}")
            mt5.shutdown()
            continue
        info = mt5.account_info()
        if info is None:
            print(f"{label}: connected to terminal but NO account logged in")
        else:
            print(f"{label}: login={info.login} server={info.server} balance={info.balance:.2f}")
        mt5.shutdown()


if __name__ == "__main__":
    main()
