"""Emergency stop: if this file exists, the bot refuses to place any trade.

Trip it from the running server (or a remote script) with:
    touch KILL_SWITCH
Remove the file to resume trading.
"""
from __future__ import annotations

import logging
from pathlib import Path

from bot.config import KillSwitchConfig, PROJECT_ROOT

logger = logging.getLogger("bot.kill_switch")


class KillSwitch:
    def __init__(self, config: KillSwitchConfig):
        self._path = PROJECT_ROOT / config.file_path

    def is_active(self) -> bool:
        return self._path.exists()

    def activate(self, reason: str) -> None:
        self._path.write_text(reason)
        logger.critical("KILL SWITCH ACTIVATED: %s", reason)

    def deactivate(self) -> None:
        if self._path.exists():
            self._path.unlink()
            logger.info("Kill switch deactivated")
