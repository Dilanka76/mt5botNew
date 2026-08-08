"""App-wide logging setup, plus a structured per-decision log.

Two separate logs are produced under logging.log_dir/<account>:
- app.log        human-readable log of everything the bot does
- decisions.jsonl  one JSON line per strategy evaluation: symbol, whether a
                    trade was taken or skipped, and why (for later review)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

from bot.config import LoggingConfig, PROJECT_ROOT

_decision_logger: logging.Logger | None = None


def setup_logging(config: LoggingConfig, account: str) -> None:
    log_dir = PROJECT_ROOT / config.log_dir / account
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bot")
    root.setLevel(config.level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    global _decision_logger
    _decision_logger = logging.getLogger("bot.decisions")
    _decision_logger.setLevel(logging.INFO)
    _decision_logger.handlers.clear()
    _decision_logger.propagate = False

    decision_handler = logging.handlers.RotatingFileHandler(
        log_dir / "decisions.jsonl", maxBytes=5_000_000, backupCount=5
    )
    decision_handler.setFormatter(logging.Formatter("%(message)s"))
    _decision_logger.addHandler(decision_handler)


def log_decision(
    symbol: str,
    action: str,  # "trade_taken" | "trade_skipped" | "signal_skipped_risk" | "signal_skipped_kill_switch"
    reason: str,
    **extra,
) -> None:
    """Records one strategy-evaluation outcome, for later review."""
    if _decision_logger is None:
        raise RuntimeError("setup_logging() must be called before log_decision()")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "reason": reason,
        **extra,
    }
    _decision_logger.info(json.dumps(entry))
