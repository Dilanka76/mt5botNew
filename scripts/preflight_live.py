"""Everything that can differ between a demo and a real account.

User, 2026-09-13, before live2 trades real money on 2026-09-14: *"can you
deeply analyze and look, is there can happen any unexpected thing in the
live account"*.

The strategy is proven on demo. What is NOT proven is that the BROKER
behaves the same, and every check here is something that is identical on
demo and can differ on live:

  MARGIN      the one that can end the account. Two legs on one login can
              hold two positions at once, and gold is a large contract:
              0.04 lots is 4 oz, about $17,000 of notional. At 1:100 that
              needs ~$174 of margin. On a $300 account, two positions can
              use most of the free margin, and a stop-out closes trades
              the strategy never chose to close.

  STOPS LEVEL the minimum distance the broker allows between price and a
              stop. The TP-runner locks its stop AT the target -- so at
              the instant of locking the distance is nearly zero. If the
              broker enforces any stops level, every lock is REJECTED and
              the runner silently degrades to a software-only stop. It
              still works; it just loses its broker-side protection, which
              is the one thing that survives the bot dying.

  FREEZE LEVEL how close to price an order can no longer be modified. The
              runner REMOVES the broker take-profit a dollar before the
              target. Inside the freeze level that removal fails, and the
              trade closes at the target as it always did.

  SPREAD      demo XAUUSDp and live XAUUSD are different instruments with
              different spreads. Every trade pays it twice.

  FILLING     a broker that does not support the filling mode the executor
              requests rejects every order.

    python scripts/preflight_live.py --accounts live2_m3,live2_m5

Read-only: reads account and symbol properties. Places nothing.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")

import MetaTrader5 as mt5

from bot.analytics import StaleTickError
from bot.config import load_config, validate_account_name
from bot.mt5_connector import MT5Connector
from bot.risk.position_sizing import calculate_lots

FAIL, WARN, OK = "FAIL", "WARN", "ok  "


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]
    problems: list[str] = []

    def check(level: str, label: str, detail: str = "") -> None:
        print(f"  [{level}] {label}")
        if detail:
            for line in detail.split("\n"):
                print(f"         {line}")
        if level in (FAIL, WARN):
            problems.append(f"{level}: {label}")

    total_margin = 0.0
    equity = None
    for account in accounts:
        c = load_config(account)
        connector = MT5Connector(c.mt5)
        connector.connect()
        try:
            info = connector.account_info()
            # SELECT it first, exactly as the bot does at startup
            # (MT5Connector.ensure_symbol). MT5 only streams ticks for
            # symbols in Market Watch, so on a brand-new account -- where
            # nothing has ever been selected -- symbol_info() returns full
            # specs while symbol_info_tick() returns None. This script then
            # reported "market closed, spread not checked" on a perfectly
            # open market, which is exactly the number the 2026-09-16 account
            # move existed to measure.
            try:
                connector.ensure_symbol(c.symbol)
            except Exception:  # noqa: BLE001 - the miss is reported below
                pass
            sym = connector.symbol_info(c.symbol)

            print("=" * 84)
            print(f"{account}   {c.symbol}   login {info.login}   {c.execution.mode}")
            print("=" * 84)

            # SYMBOL FIRST. Everything below needs a price, and a price needs
            # the symbol to exist. Reading sym.ask before checking that
            # crashed with AttributeError on NoneType -- so the one situation
            # this script exists to diagnose (a new account that names gold
            # differently) was the one it could not report. 2026-09-16, while
            # moving live2 to a tighter-spread account.
            if sym is None:
                check(FAIL, f"symbol {c.symbol} does NOT exist on this account")
                try:
                    gold = sorted(s.name for s in (mt5.symbols_get() or [])
                                  if "XAU" in s.name.upper() or "GOLD" in s.name.upper())
                except Exception:  # noqa: BLE001 - reporting must not raise
                    gold = []
                if gold:
                    print("         gold symbols this account DOES offer:")
                    for name in gold:
                        print(f"           {name}")
                    print(f"         Put the right one in config/settings.{account}.yaml")
                    print("         under `symbol:` — then re-run this.")
                else:
                    print("         and no gold symbol was found at all. If the account is new,")
                    print("         open Market Watch in the terminal and show the gold symbol")
                    print("         first — MT5 hides symbols that have never been selected.")
                continue

            tick = mt5.symbol_info_tick(c.symbol)
            price = float(tick.ask) if tick and tick.ask else float(sym.ask or 0)

            # ---- is this actually a real account? ----
            # trade_mode 0 = demo, 1 = contest, 2 = real
            real = getattr(info, "trade_mode", None) == 2
            if c.execution.mode == "live_execute" and not real:
                check(WARN, f"config says live_execute but MT5 reports trade_mode="
                            f"{getattr(info, 'trade_mode', '?')} (2 = real)")
            elif real:
                check(OK, "MT5 confirms this is a REAL account")

            # ---- the three switches that silently reject every order ----
            #
            # None of these can be set from code. They live in the terminal
            # and at the broker, and when one is off order_send fails while
            # everything else -- connection, symbol, margin, config -- looks
            # perfect. On 2026-08-27 a manual-trade rejection was traced back
            # to AutoTrading being off after hours of looking elsewhere, and
            # the only trace of that in this project was a COMMENT. Checked
            # here so a new terminal cannot repeat it.
            term = mt5.terminal_info()
            if term is None:
                check(WARN, "could not read terminal_info() — AutoTrading state unknown")
            elif not getattr(term, "trade_allowed", False):
                check(FAIL, "AutoTrading is OFF in the terminal",
                      "The 'Algo Trading' button is not green. Every order will be\n"
                      "rejected. Click it in the terminal, or Tools > Options >\n"
                      "Expert Advisors > Allow algorithmic trading. Code cannot set this.")
            else:
                check(OK, "AutoTrading is ON in the terminal (Algo Trading is green)")

            if not getattr(info, "trade_allowed", True):
                check(FAIL, "the BROKER has disabled trading on this account",
                      "Nothing on this machine can fix that — contact the broker.")
            elif not getattr(info, "trade_expert", True):
                check(FAIL, "the BROKER has disabled EXPERT ADVISOR trading on this account",
                      "Manual trades would work; automated ones will not. This is the one\n"
                      "that looks like the bot is broken when it is the account setting.")
            else:
                check(OK, "the broker allows automated trading on this account")

            # ---- margin: the one that can end the account ----
            lots = calculate_lots(info.balance, c.position_sizing)
            margin = None
            if price:
                margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, c.symbol, lots, price)
            if margin is None:
                check(WARN, "could not compute margin (market may be closed)")
            else:
                total_margin += margin
                equity = info.equity
                pct = 100 * margin / info.equity if info.equity else 0
                notional = lots * sym.trade_contract_size * price
                lvl = FAIL if pct > 40 else WARN if pct > 20 else OK
                check(lvl, f"margin for {lots} lots: ${margin:,.2f} "
                           f"({pct:.0f}% of ${info.equity:,.2f} equity)",
                      f"leverage 1:{info.leverage}   notional ${notional:,.0f}")

            # ---- stops level vs what the runner asks for ----
            stops_usd = sym.trade_stops_level * sym.point
            freeze_usd = sym.trade_freeze_level * sym.point
            if c.tp_runner_trail_usd is not None:
                if stops_usd <= 0:
                    check(OK, "stops level is 0 — the runner can lock its stop at the target")
                else:
                    check(WARN, f"stops level ${stops_usd:.2f} — the runner locks AT the target, "
                                f"so that stop sits ~$0 from price and will be REJECTED",
                          "the software stop still fires, but the position loses broker-side\n"
                          "protection if the bot dies. Expect 'broker stop REJECTED' in the log.")
                arm = c.take_profit_usd - c.tp_runner_arm_before_usd
                if freeze_usd > c.tp_runner_arm_before_usd:
                    check(WARN, f"freeze level ${freeze_usd:.2f} exceeds the ${c.tp_runner_arm_before_usd:.2f} "
                                f"arm window — the take-profit removal will fail",
                          "trades then close at the target as if the runner were off.")
                else:
                    check(OK, f"freeze level ${freeze_usd:.2f} is inside the "
                              f"${c.tp_runner_arm_before_usd:.2f} arm window")

            # ---- the stop itself ----
            if c.stop_loss_usd is not None and stops_usd > c.stop_loss_usd:
                check(FAIL, f"stops level ${stops_usd:.2f} is WIDER than the "
                            f"${c.stop_loss_usd:.2f} stop — a broker stop can never be placed")

            # ---- spread, paid twice per trade ----
            if tick and tick.ask and tick.bid:
                spread = tick.ask - tick.bid
                target = c.take_profit_usd
                pct = 100 * spread / target if target else 0
                lvl = WARN if pct > 8 else OK
                check(lvl, f"spread ${spread:.2f} = {pct:.1f}% of the ${target:.2f} target")
            else:
                check(WARN, "no live tick — market closed, spread not checked",
                      "re-run once trading opens; live spread can differ from demo.")

            # ---- can we trade this symbol at all? ----
            if getattr(sym, "trade_mode", 4) == 0:
                check(FAIL, f"{c.symbol} is DISABLED for trading on this account")
            else:
                check(OK, f"{c.symbol} tradeable, min lot {sym.volume_min}, "
                          f"step {sym.volume_step}")
            if lots < sym.volume_min:
                check(FAIL, f"the ladder wants {lots} lots but the broker minimum is "
                            f"{sym.volume_min}")

            # ---- the daily limit against this balance ----
            if c.daily_loss_limit_usd:
                pct = 100 * c.daily_loss_limit_usd / info.balance if info.balance else 0
                lvl = FAIL if pct > 33 else WARN if pct > 15 else OK
                check(lvl, f"daily loss limit ${c.daily_loss_limit_usd:.0f} = {pct:.0f}% "
                           f"of the ${info.balance:,.2f} balance")
            else:
                check(FAIL, "NO daily loss limit on a live account")
            print()
        finally:
            connector.disconnect()

    # ---- both legs at once, which is the real exposure ----
    if len(accounts) > 1 and total_margin and equity:
        pct = 100 * total_margin / equity
        print("=" * 84)
        lvl = FAIL if pct > 50 else WARN if pct > 30 else OK
        print(f"  [{lvl}] BOTH legs holding a position: ${total_margin:,.2f} margin "
              f"= {pct:.0f}% of equity")
        print(f"         Free margin left: ${equity - total_margin:,.2f}. When free margin runs")
        print(f"         out the broker closes positions itself, at prices the strategy")
        print(f"         never chose. That is the one failure mode no rule here can stop.")
        if pct > 30:
            problems.append("both legs' combined margin")
        print()

    print("=" * 84)
    if problems:
        print(f"{len(problems)} thing(s) to look at before trading real money:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("Nothing flagged. Re-run once the market is open — spread and margin")
        print("cannot be measured with no ticks flowing.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED for part of this check — margin and spread need live ticks.")
        print("Everything else above still stands. Re-run once trading opens.")
        print(f"\n  {exc}")
        raise SystemExit(1)
