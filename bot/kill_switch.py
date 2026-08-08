"""Emergency stop: if this file exists, the bot refuses to place any trade.
Per-account — each account has its own KILL_SWITCH_<account> file, so
stopping one account never affects the others.

Trip it from the running server (or a remote script) with:
    touch KILL_SWITCH_demo1
Remove the file to resume trading.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot.config import KillSwitchConfig, PROJECT_ROOT

logger = logging.getLogger("bot.kill_switch")


class KillSwitch:
    def __init__(self, config: KillSwitchConfig, account: str):
        self._path = PROJECT_ROOT / f"{config.file_path}_{account}"

    def is_active(self) -> bool:
        return self._path.exists()

    def activate(self, reason: str) -> None:
        self._path.write_text(reason)
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def deactivate(self) -> None:
        if self._path.exists():
            self._path.unlink()
            logger.info("Kill switch deactivated")
