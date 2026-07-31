"""Loads config/settings.yaml + .env into typed config objects."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MT5Config:
    login: int | None
    password: str | None
    server: str | None
    terminal_path: str | None
    timeout_ms: int


@dataclass
class EMAPeriodsConfig:
    fast: int
    mid: int
    slow: int


@dataclass
class SessionWindow:
    start: str  # "HH:MM", Asia/Colombo local time
    end: str


@dataclass
class PositionSizingTier:
    max_balance: float | None  # None = no upper bound (this is the top tier)
    lots: float


@dataclass
class ExecutionConfig:
    mode: str  # shadow | demo_execute | live_execute
    require_demo_account: bool
    magic_number: int
    order_deviation_points: int
    order_comment: str


@dataclass
class LoggingConfig:
    log_dir: str
    level: str


@dataclass
class KillSwitchConfig:
    file_path: str


@dataclass
class AppConfig:
    mt5: MT5Config
    symbol: str
    timeframe: str
    candles_to_fetch: int
    tick_poll_interval_seconds: int
    ema_periods: EMAPeriodsConfig
    gap_threshold_usd: float
    take_profit_usd: float
    sessions: list[SessionWindow]
    position_sizing: list[PositionSizingTier]
    execution: ExecutionConfig
    logging: LoggingConfig
    kill_switch: KillSwitchConfig


def load_config(settings_path: str | Path = PROJECT_ROOT / "config" / "settings.yaml") -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")

    with open(settings_path, "r") as f:
        raw = yaml.safe_load(f)

    mt5_raw = raw.get("mt5", {})
    mt5_login = os.getenv("MT5_LOGIN")

    return AppConfig(
        mt5=MT5Config(
            login=int(mt5_login) if mt5_login else None,
            password=os.getenv("MT5_PASSWORD") or None,
            server=os.getenv("MT5_SERVER") or None,
            terminal_path=os.getenv("MT5_TERMINAL_PATH") or None,
            timeout_ms=mt5_raw.get("timeout_ms", 10000),
        ),
        symbol=raw["symbol"],
        timeframe=raw["timeframe"],
        candles_to_fetch=raw["candles_to_fetch"],
        tick_poll_interval_seconds=raw["tick_poll_interval_seconds"],
        ema_periods=EMAPeriodsConfig(**raw["ema_periods"]),
        gap_threshold_usd=raw["gap_threshold_usd"],
        take_profit_usd=raw["take_profit_usd"],
        sessions=[SessionWindow(**s) for s in raw["sessions"]],
        position_sizing=[PositionSizingTier(**t) for t in raw["position_sizing"]],
        execution=ExecutionConfig(**raw["execution"]),
        logging=LoggingConfig(**raw["logging"]),
        kill_switch=KillSwitchConfig(**raw["kill_switch"]),
    )
