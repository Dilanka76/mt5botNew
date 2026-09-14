"""close_position must actually send a close order.

    python3 tests/test_close_position_request.py

REAL INCIDENT, 2026-09-13 12:10 IST (commit a412f9e, the broker-backstop
change). Two lines belonging to open_market_order were pasted into
close_position as well:

    if stop_loss is not None:
        request["sl"] = stop_loss

`stop_loss` is a local of open_market_order. In close_position it does not
exist, so the function raised NameError before reaching order_send -- on
EVERY call, for the next eighteen hours, on every account sharing this
executor.

Everything that closes a trade in software went with it: the breakeven
stop, the stop-loss, the opposite-cross swap, the foreign-position
rejection, the validation-failed close. Only broker-side exits still
worked -- a take-profit fill, or the backstop. It surfaced as a trade
stranded for 2h32m on demo1_m3 (tests/test_close_failure_orphan.py) and
looked like a TP-runner fault for most of a morning.

py_compile does not catch an undefined name; only running the line does.
So this test runs it.
"""
from __future__ import annotations

import sys
import types

sys.path.insert(0, ".")

_stub = types.ModuleType("MetaTrader5")
_stub.ORDER_TYPE_BUY = 0
_stub.ORDER_TYPE_SELL = 1
_stub.TRADE_ACTION_DEAL = 1
_stub.ORDER_TIME_GTC = 0
_stub.ORDER_FILLING_IOC = 1
_stub.TRADE_RETCODE_DONE = 10009
_stub.last_error = lambda: (0, "ok")
for _i, _tf in enumerate(["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1"]):
    setattr(_stub, f"TIMEFRAME_{_tf}", _i + 1)
sys.modules.setdefault("MetaTrader5", _stub)
_dt = types.ModuleType("dotenv"); _dt.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dt)

from bot.execution.trade_executor import TradeExecutor

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


sent: list[dict] = []


def main() -> None:
    _stub.positions_get = lambda ticket=None, **kw: [types.SimpleNamespace(
        ticket=203845694, symbol="XAUUSDp", volume=0.06, type=_stub.ORDER_TYPE_SELL,
        sl=4342.19, tp=0.0)]

    def order_send(request):
        sent.append(request)
        return types.SimpleNamespace(retcode=_stub.TRADE_RETCODE_DONE, order=1)
    _stub.order_send = order_send

    connector = types.SimpleNamespace(
        get_tick=lambda symbol: types.SimpleNamespace(bid=4330.01, ask=4330.23))
    config = types.SimpleNamespace(
        mode="live_execute", order_deviation_points=20, magic_number=910003,
        order_comment="ema-scalp")
    executor = TradeExecutor(config, connector, "XAUUSDp")

    print("close_position actually runs")
    try:
        executor.close_position(203845694)
        raised = None
    except NameError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001 - any other failure is also a failure
        raised = exc
    check("it does not raise (NameError killed every close for 18 hours)",
          raised is None)
    if raised is not None:
        print(f"        raised: {type(raised).__name__}: {raised}")

    check("an order was actually submitted", len(sent) == 1)
    if sent:
        request = sent[0]
        print("\nthe close request is shaped correctly")
        check("it carries NO stop-loss -- a close protects nothing",
              "sl" not in request)
        check("it closes the right ticket", request.get("position") == 203845694)
        check("closing a SELL buys back", request.get("type") == _stub.ORDER_TYPE_BUY)
        check("a SELL is bought back at the ASK", request.get("price") == 4330.23)
        check("it closes the whole volume", request.get("volume") == 0.06)

    print("\nshadow mode still sends nothing")
    sent.clear()
    shadow = TradeExecutor(
        types.SimpleNamespace(mode="shadow", order_deviation_points=20,
                              magic_number=910003, order_comment="ema-scalp"),
        connector, "XAUUSDp")
    shadow.close_position(203845694)
    check("shadow mode places no order", not sent)

    print("\na ticket the broker no longer has is not an error")
    sent.clear()
    _stub.positions_get = lambda ticket=None, **kw: []
    executor.close_position(203845694)
    check("already-closed ticket returns quietly", not sent)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
