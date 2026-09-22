"""Every trade the bot opened must be counted, however it was CLOSED.

    python3 tests/test_closed_trades_pairing.py

REAL GAP, found 2026-09-22: bot.analytics paired deals by the bot's magic
number, but a close from the MT5 phone app, desktop or web carries magic
0. live2 had 12 hand-closed trades that the broker showed and every report
built on get_closed_trades_range() silently left out -- 41 scripts,
including the stop, loss and entry research. Partial closes were cut
short too: a 0.03-lot trade closed in two parts showed as 0.01 lots.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

mt5 = types.ModuleType("MetaTrader5")
for name, value in dict(DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=3,
                        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
                        DEAL_REASON_SL=4, DEAL_REASON_TP=5).items():
    setattr(mt5, name, value)
mt5.__getattr__ = lambda name: 0                     # type: ignore[attr-defined]
sys.modules["MetaTrader5"] = mt5

import bot.analytics as analytics                      # noqa: E402

failures: list[str] = []
BOT = 950005
OFFSET = timedelta(hours=3)
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def deal(pid, entry, magic, minutes, volume, price, profit=0.0, commission=0.0,
         reason=3, comment="", type_=0):
    t = int((T0 + timedelta(minutes=minutes) + OFFSET).timestamp())   # broker clock
    return NS(position_id=pid, symbol="XAUUSDp", entry=entry, magic=magic, time=t,
              volume=volume, price=price, profit=profit, swap=0.0, commission=commission,
              reason=reason, comment=comment, type=type_)


DEALS = [
    # 1: the bot opens, the user closes from the PHONE (magic 0, reason 1)
    deal(1, 0, BOT, 0, 0.02, 4350.0, commission=-0.12),
    deal(1, 1, 0, 30, 0.02, 4346.0, profit=-8.0, reason=1),
    # 2: closed in two parts -- 0.02 by hand, then 0.01 by the bot
    deal(2, 0, BOT, 60, 0.03, 4332.23, commission=-0.18),
    deal(2, 1, 0, 90, 0.02, 4340.0, profit=15.54, reason=1),
    deal(2, 1, BOT, 120, 0.01, 4341.93, profit=9.70, comment="dual-cross-close"),
    # 3: partly closed and STILL OPEN -- not a closed trade
    deal(3, 0, BOT, 150, 0.03, 4300.0),
    deal(3, 1, 0, 160, 0.01, 4302.0, profit=2.0, reason=1),
    # 4: someone else's position (opened by hand) -- never ours
    deal(4, 0, 0, 170, 0.01, 4310.0),
    deal(4, 1, 0, 175, 0.01, 4311.0, profit=1.0, reason=1),
    # 5: an ordinary bot trade, closed at take-profit by the broker
    deal(5, 0, BOT, 200, 0.02, 4350.0, commission=-0.12, type_=1),
    deal(5, 1, BOT, 220, 0.02, 4342.0, profit=16.0, reason=5),
]


def main() -> None:
    analytics.mt5.history_deals_get = lambda a, b: DEALS
    trades = {t["position_id"]: t for t in analytics.get_closed_trades_range(
        "XAUUSDp", BOT, T0 - timedelta(hours=1), T0 + timedelta(hours=6), OFFSET)}

    print("closed from the phone")
    check("the trade is found at all", 1 in trades)
    if 1 in trades:
        check("it is labelled as closed by hand (reason 1)", "reason=1" in trades[1]["exit_reason"])
        check("its loss includes the entry commission", abs(trades[1]["profit"] - (-8.12)) < 1e-9)

    print("\nclosed in two parts")
    check("the trade is found", 2 in trades)
    if 2 in trades:
        t = trades[2]
        check("full size, not the last part (0.03 lots)", abs(t["volume"] - 0.03) < 1e-9)
        check("profit sums every part (+$25.06)", abs(t["profit"] - (15.54 + 9.70 - 0.18)) < 1e-9)
        want = (4340.0 * 0.02 + 4341.93 * 0.01) / 0.03
        check("exit price is the volume-weighted average", abs(t["exit_price"] - want) < 1e-9)
        check("labelled by its LAST exit (the bot's cross exit)", t["exit_reason"] == "EMA Cross Exit")
        check("and says there were two exits", t["exit_deals"] == 2)

    print("\nwhat must stay out")
    check("a trade still partly open is not reported as closed", 3 not in trades)
    check("a position the bot never opened is not ours", 4 not in trades)

    print("\nan ordinary bot trade is unchanged")
    check("found, take-profit", 5 in trades and trades[5]["exit_reason"] == "Take Profit")
    check("profit as before (+$15.88)", 5 in trades and abs(trades[5]["profit"] - 15.88) < 1e-9)
    check("direction read from the entry", 5 in trades and trades[5]["direction"] == "SELL")

    print("\nthe single-day version agrees")
    day = analytics.get_closed_trades("XAUUSDp", BOT, (T0 + timedelta(hours=5, minutes=30)).date(), OFFSET)
    check("it finds the hand-closed and part-closed trades too",
          {1, 2, 5} <= {t["position_id"] for t in day})

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
