"""Entry point for the EMA-cross scalper.

Not wired up yet: the full trade loop (session gating, EMA5-touch waiting,
one-position state machine, execution) is a later increment — see
/Users/dilankaamarakoon/.claude/plans/i-m-building-an-automated-toasty-pond.md.

For now, use scripts/check_crosses.py to verify EMA calculation and cross
detection against your own MT5 chart before this file gets the real loop.
"""
from __future__ import annotations

import logging

from bot.config import load_config
from bot.logging_setup.logger import setup_logging
from bot.mt5_connector import MT5Connector

logger = logging.getLogger("bot.main")


def run() -> None:
    config = load_config()
    setup_logging(config.logging)

    connector = MT5Connector(config.mt5)
    connector.connect()

    if config.execution.require_demo_account and not connector.is_demo_account():
        connector.disconnect()
        raise RuntimeError(
            "require_demo_account is true but the connected MT5 account is not a demo account. Aborting."
        )

    logger.info(
        "Connected. symbol=%s timeframe=%s mode=%s. "
        "Trade loop not implemented yet — run scripts/check_crosses.py to verify EMA/cross logic.",
        config.symbol,
        config.timeframe,
        config.execution.mode,
    )
    connector.disconnect()


if __name__ == "__main__":
    run()
