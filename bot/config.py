"""Loads config/settings.<account>.yaml + .env.<account> into typed config
objects. Every account (demo1, live1, ...) gets its own settings file and
env file so accounts are fully independent — see SETUP.md."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_account_name(name: str) -> str:
    """Shared --account validator (used as argparse `type=`) — account names
    flow straight into file paths (.env.<account>, settings.<account>.yaml,
    logs/<account>/, KILL_SWITCH_<account>), so this keeps them restricted
    to a safe charset instead of allowing path separators or traversal."""
    if not ACCOUNT_NAME_RE.match(name):
        raise ValueError(
            f"Invalid account name {name!r}. Only letters, digits, '-' and '_' are allowed."
        )
    return name


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
    account: str
    mt5: MT5Config
    symbol: str
    timeframe: str
    candles_to_fetch: int
    tick_poll_interval_seconds: int
    ema_periods: EMAPeriodsConfig
    gap_threshold_usd: float
    take_profit_usd: float
    strategy_variant: str  # "gap_threshold" | "ema5_only"
    sessions: dict[str, list[SessionWindow]]  # keyed by strategy_variant — each variant has its own schedule
    position_sizing: list[PositionSizingTier]
    execution: ExecutionConfig
    logging: LoggingConfig
    kill_switch: KillSwitchConfig


def load_config(account: str, settings_path: str | Path | None = None) -> AppConfig:
    """Loads the account-scoped config. `account` selects .env.<account> and
    config/settings.<account>.yaml (unless settings_path overrides the latter) —
    see SETUP.md. Every account is fully independent: its own MT5 credentials,
    lot sizing, sessions, logs, and kill switch."""
    account = validate_account_name(account)

    env_path = PROJECT_ROOT / f".env.{account}"
    if not env_path.exists():
        raise FileNotFoundError(
            f"{env_path} not found. Copy .env.demo1.example to {env_path.name} and fill in "
            f"real values (see SETUP.md)."
        )
    # override=True: an account's own .env.<account> must always win over
    # anything already in the environment (a stale OS-level var, or another
    # account's .env loaded earlier in the same process) — accounts must
    # stay fully independent, per SETUP.md.
    load_dotenv(env_path, override=True)

    if settings_path is None:
        settings_path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
    settings_path = Path(settings_path)
    if not settings_path.exists():
        raise FileNotFoundError(
            f"{settings_path} not found. Copy config/settings.demo1.example.yaml to "
            f"{settings_path.name} and adjust as needed (see SETUP.md)."
        )

    with open(settings_path, "r") as f:
        raw = yaml.safe_load(f)

    mt5_raw = raw.get("mt5", {})
    mt5_login = os.getenv("MT5_LOGIN")

    strategy_variant = raw.get("strategy_variant", "gap_threshold")
    sessions_raw = raw["sessions"]
    if strategy_variant not in sessions_raw:
        raise ValueError(
            f"{settings_path}: strategy_variant '{strategy_variant}' has no matching "
            f"entry under 'sessions:'. Available: {list(sessions_raw)}"
        )

    return AppConfig(
        account=account,
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
        strategy_variant=strategy_variant,
        sessions={
            variant: [SessionWindow(**s) for s in windows]
            for variant, windows in sessions_raw.items()
        },
        position_sizing=[PositionSizingTier(**t) for t in raw["position_sizing"]],
        execution=ExecutionConfig(**raw["execution"]),
        logging=LoggingConfig(**raw["logging"]),
        kill_switch=KillSwitchConfig(**raw["kill_switch"]),
    )


SETTINGS_FILENAME_RE = re.compile(r"^settings\.([A-Za-z0-9_-]+)\.yaml$")


def discover_configured_accounts() -> list[str]:
    """Account names with a settings.<account>.yaml present in config/ —
    used by api_server.py's unified gateway to find out which accounts to
    serve without hardcoding the list. Deliberately excludes
    *.example.yaml templates and the legacy pre-multi-account
    config/settings.yaml (neither matches the ACCOUNT_NAME_RE-validated
    group this regex requires)."""
    accounts = []
    for path in (PROJECT_ROOT / "config").glob("settings.*.yaml"):
        match = SETTINGS_FILENAME_RE.match(path.name)
        if match is None:
            continue  # e.g. settings.demo1.example.yaml, or a malformed name
        account = match.group(1)
        try:
            accounts.append(validate_account_name(account))
        except ValueError:
            continue
    return sorted(accounts)
