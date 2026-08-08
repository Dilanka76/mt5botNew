"""Periodic per-account status snapshot — written by main.py, read by
api_server.py's unified gateway.

Deliberately pure/MT5-free: this module must never import anything that
imports MetaTrader5 (directly or transitively), because api_server.py
imports this module and MUST stay importable on a machine with no MT5
package at all. main.py is the one place that touches MT5 — it converts
whatever it gets from bot.mt5_connector/bot.execution.trade_executor into
plain dicts/primitives BEFORE calling build_status_payload() here.

Not a substitute for logs/<account>/app.log — this is a small, current
snapshot for a remote status check (e.g. a mobile app), not a history.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from bot.config import PROJECT_ROOT

# Matches scripts/watchdog.py's default --stale-threshold and
# scripts/health_check.py's STALE_HEARTBEAT_THRESHOLD_SECONDS — one
# definition of "how old is too old" across the codebase.
STATUS_STALE_THRESHOLD_SECONDS = 180

_WRITE_RETRY_ATTEMPTS = 3
_WRITE_RETRY_DELAY_SECONDS = 0.05


def status_file_path(log_dir: str, account: str) -> Path:
    return PROJECT_ROOT / log_dir / account / "status.json"


def build_status_payload(
    *,
    account: str,
    bot_state: str,
    session_status: str,
    execution_mode: str,
    symbol: str,
    kill_switch_active: bool,
    account_info: dict | None,
    tick: dict | None,
    open_position: dict | None,
    recent_closed_trades: list[dict],
) -> dict:
    """Pure formatting — every argument is already a plain primitive/dict,
    never a raw MT5 object, so this function has no MT5 dependency at all."""
    return {
        "account": account,
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "bot_state": bot_state,
        "session_status": session_status,
        "execution_mode": execution_mode,
        "symbol": symbol,
        "kill_switch_active": kill_switch_active,
        "account_info": account_info,
        "tick": tick,
        "open_position": open_position,
        "recent_closed_trades": recent_closed_trades,
    }


def write_status_atomic(path: Path, payload: dict) -> None:
    """Writes `payload` as JSON to `path`, atomically — readers (the
    gateway) only ever see a fully-old or fully-new file, never a
    partially-written one. The temp file MUST live in the same directory
    as the target: os.replace() is only atomic within one filesystem/volume.

    A short retry loop guards against a transient Windows sharing
    violation (e.g. AV briefly holding the file open right after a
    replace) — cheap insurance given this whole feature exists to
    eliminate hangs/flakiness, not add new ones."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    data = json.dumps(payload, indent=2)

    last_error: OSError | None = None
    for attempt in range(_WRITE_RETRY_ATTEMPTS):
        try:
            tmp_path.write_text(data)
            os.replace(tmp_path, path)
            return
        except OSError as e:
            last_error = e
            if attempt < _WRITE_RETRY_ATTEMPTS - 1:
                time.sleep(_WRITE_RETRY_DELAY_SECONDS)

    raise last_error


def read_status(path: Path) -> tuple[dict | None, float | None]:
    """Returns (payload, age_seconds) for the gateway's read side.
    (None, None) if the file doesn't exist or fails to parse — callers
    should treat that as "no data yet", not an error, since main.py may
    simply not have written a first snapshot yet (or ever, for an older
    build)."""
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    age_seconds = max(0.0, time.time() - path.stat().st_mtime)
    return payload, age_seconds
