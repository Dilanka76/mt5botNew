"""A minimal stand-in for MT5Connector, used only by the backtest runner.

EMAScalpEngine/TradeExecutor in shadow mode touch exactly two connector
methods — self.connector.account_info().balance (state_machine.py's
_enter(), the only direct connector call anywhere in the engine) and
self.connector.get_tick(symbol) (trade_executor.py's open_market_order,
called unconditionally before the shadow-mode early-return). Nothing else
is needed: shadow mode's open_market_order/close_position never reach
real MT5, and get_open_position()/get_all_positions() call mt5.positions_get()
directly rather than through the connector, so this class can't and
doesn't need to intercept those (the backtest runner avoids triggering
them at all — see runner.py).

Real MT5 objects are duck-typed (not subclassed) throughout this codebase
(bot/mt5_connector.py just returns raw mt5.* structs) — these plain
dataclasses match that pattern, providing only the attributes actually
read: .bid/.ask on a tick, .balance on account info.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BacktestTick:
    bid: float
    ask: float


@dataclass
class BacktestAccountInfo:
    balance: float


class BacktestConnector:
    """The runner mutates .bid/.ask/.balance directly before each engine
    call to reflect the currently-simulated historical moment — there's
    no live connection to poll, so "get" here just returns whatever the
    runner most recently set."""

    def __init__(self, starting_balance: float):
        self.bid = 0.0
        self.ask = 0.0
        self.balance = starting_balance

    def get_tick(self, symbol: str) -> BacktestTick:
        return BacktestTick(bid=self.bid, ask=self.ask)

    def account_info(self) -> BacktestAccountInfo:
        return BacktestAccountInfo(balance=self.balance)
