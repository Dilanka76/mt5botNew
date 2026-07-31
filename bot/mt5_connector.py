"""Handles connecting to the MT5 terminal and basic account/symbol lookups."""
from __future__ import annotations

import logging

import MetaTrader5 as mt5

from bot.config import MT5Config

logger = logging.getLogger("bot.mt5")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


class MT5ConnectionError(RuntimeError):
    pass


class MT5Connector:
    def __init__(self, config: MT5Config):
        self.config = config
        self._connected = False

    def connect(self) -> None:
        init_kwargs = {"timeout": self.config.timeout_ms}
        if self.config.terminal_path:
            init_kwargs["path"] = self.config.terminal_path

        if not mt5.initialize(**init_kwargs):
            raise MT5ConnectionError(f"mt5.initialize() failed: {mt5.last_error()}")

        # Only call mt5.login() if we actually need a different account than
        # whatever the terminal is already authenticated as. Calling login()
        # again right after initialize() when the terminal is already logged
        # in (e.g. via the GUI, as in our setup) is a known source of a
        # spurious "mt5.login() failed: (1, 'Success')" — login() returns
        # False without last_error() recording a real reason.
        wants_login = self.config.login and self.config.password and self.config.server
        current_account = mt5.account_info()
        already_correct_account = current_account is not None and (
            not self.config.login or current_account.login == self.config.login
        )

        if wants_login and not already_correct_account:
            authorized = mt5.login(
                self.config.login,
                password=self.config.password,
                server=self.config.server,
                timeout=self.config.timeout_ms,
            )
            if not authorized:
                mt5.shutdown()
                raise MT5ConnectionError(f"mt5.login() failed: {mt5.last_error()}")

        account_info = mt5.account_info()
        if account_info is None:
            mt5.shutdown()
            raise MT5ConnectionError(
                "Connected to terminal but no account is logged in. "
                "Log into MT5 manually or set MT5_LOGIN/MT5_PASSWORD/MT5_SERVER in .env."
            )

        self._connected = True
        logger.info(
            "Connected to MT5: login=%s server=%s balance=%.2f trade_mode=%s",
            account_info.login,
            account_info.server,
            account_info.balance,
            account_info.trade_mode,
        )

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("Disconnected from MT5")

    def is_demo_account(self) -> bool:
        account_info = mt5.account_info()
        if account_info is None:
            raise MT5ConnectionError("Not connected to MT5")
        return account_info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO

    def account_info(self):
        info = mt5.account_info()
        if info is None:
            raise MT5ConnectionError("Not connected to MT5")
        return info

    def ensure_symbol(self, symbol: str) -> None:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5ConnectionError(f"Symbol not found: {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise MT5ConnectionError(f"Could not add symbol to Market Watch: {symbol}")

    def symbol_info(self, symbol: str):
        return mt5.symbol_info(symbol)

    def get_tick(self, symbol: str):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5ConnectionError(f"No tick data for {symbol}: {mt5.last_error()}")
        return tick

    @staticmethod
    def resolve_timeframe(timeframe_str: str) -> int:
        try:
            return TIMEFRAME_MAP[timeframe_str]
        except KeyError:
            raise ValueError(
                f"Unsupported timeframe '{timeframe_str}'. Valid: {list(TIMEFRAME_MAP)}"
            )

    def __enter__(self) -> "MT5Connector":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()
