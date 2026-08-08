"""FastAPI control/monitoring server for the MT5 trading bot.

Runs alongside main.py as a SEPARATE process on the same Windows server —
intended for a future Flutter app to check status and start/stop the bot
remotely.

MULTI-ACCOUNT: this server always controls exactly one account, given via
the required --account flag (e.g. `python api_server.py --account demo1
--port 8001`). Run one instance per account you want remote control over,
each on its own --port, e.g.:
    python api_server.py --account demo1 --port 8001
    python api_server.py --account live1 --port 8002
Because --account is required, this can no longer be launched via
`uvicorn api_server:app` directly (uvicorn's own CLI has no way to forward
it) — always run it as `python api_server.py --account ... [--port ...]`.

Deliberately reads state from the outside rather than reaching into
main.py's live objects: the OS process list (is main.py running), the
decisions log (best-effort inference of IDLE vs PENDING_ENTRY), and MT5
itself (account info, open position, closed trade history) via its own
independent MT5 connection. This mirrors scripts/watchdog.py's philosophy —
if either process has a bug or dies, it doesn't take the other down. MT5
supports multiple simultaneous client connections, so this server querying
account/position/history data at the same time main.py is trading is safe;
this server never places or closes trades itself.

SECURITY: every endpoint requires the X-API-Key header to match API_KEY in
.env.<account>. That is the ONLY protection right now. Before exposing this
port to the internet (vs. just the local machine/LAN), you still need HTTPS
(e.g. a reverse proxy) and a firewall/security-group rule limiting who can
reach the port — do not point this at the internet as-is.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException

from bot.config import load_config, validate_account_name
from bot.kill_switch import KillSwitch
from bot.mt5_connector import MT5Connector
from bot.process_utils import find_account_process, launch_python_script

PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
MAIN_SCRIPT_MATCH = "main.py"  # substring matched (case-insensitive) against process command lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Per-account control/monitoring API for the MT5 trading bot.")
    parser.add_argument(
        "--account", required=True, type=validate_account_name,
        help="Account name, e.g. demo1, live1. Selects .env.<account>/config/settings.<account>.yaml and controls "
             "only that account's main.py.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to serve on (default: 8000). Use a different port per account.")
    return parser.parse_args()


args = parse_args()
ACCOUNT = args.account
PORT = args.port

load_dotenv(PROJECT_ROOT / f".env.{ACCOUNT}")
API_KEY = os.getenv("API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bot.api_server")

config = load_config(ACCOUNT)
connector = MT5Connector(config.mt5)
kill_switch = KillSwitch(config.kill_switch, ACCOUNT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    connector.connect()
    logger.info("api_server (account=%s) connected to MT5 (read-only queries)", ACCOUNT)
    yield
    connector.disconnect()


app = FastAPI(title=f"MT5 Bot Control API ({ACCOUNT})", lifespan=lifespan)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail=f"API_KEY is not configured on the server (.env.{ACCOUNT})")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# --- state readers ---

def read_last_decisions(n: int) -> list[dict]:
    path = PROJECT_ROOT / config.logging.log_dir / ACCOUNT / "decisions.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(errors="ignore").splitlines()
    parsed = []
    for line in lines[-n:]:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def infer_bot_state(position_open: bool) -> dict:
    """Best-effort state, since this is a separate process from main.py and
    can't read its live in-memory state directly. An open position is
    ground truth (from MT5); IDLE vs PENDING_ENTRY is inferred from the most
    recent relevant line in decisions.jsonl."""
    if position_open:
        return {"value": "IN_POSITION", "source": "mt5"}

    for entry in reversed(read_last_decisions(50)):
        action = entry.get("action")
        if action == "setup_pending":
            return {"value": "PENDING_ENTRY", "source": "inferred_from_logs"}
        if action in (
            "trade_entered", "trade_exited", "trade_closed_tp",
            "setup_invalidated", "entry_skipped_outside_session",
        ):
            return {"value": "IDLE", "source": "inferred_from_logs"}

    return {"value": "UNKNOWN", "source": "no_recent_log_activity"}


def get_open_position() -> dict | None:
    positions = mt5.positions_get(symbol=config.symbol)
    if not positions:
        return None
    for p in positions:
        if p.magic == config.execution.magic_number:
            return {
                "ticket": p.ticket,
                "direction": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "take_profit": p.tp,
                "profit": p.profit,
                "open_time": datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            }
    return None


def get_recent_closed_trades(limit: int = 5, lookback_days: int = 7) -> list[dict]:
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=lookback_days)
    deals = mt5.history_deals_get(date_from, date_to)
    if not deals:
        return []

    closing_deals = [
        d for d in deals
        if d.symbol == config.symbol
        and d.magic == config.execution.magic_number
        and d.entry == mt5.DEAL_ENTRY_OUT  # the deal that closes a position, carries the realized profit
    ]
    closing_deals.sort(key=lambda d: d.time, reverse=True)

    return [
        {
            "ticket": d.ticket,
            "position_id": d.position_id,
            # closing deal type is the OPPOSITE of the position that was closed
            "direction": "SELL" if d.type == mt5.DEAL_TYPE_SELL else "BUY",
            "volume": d.volume,
            "price": d.price,
            "profit": d.profit,
            "close_time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
        }
        for d in closing_deals[:limit]
    ]


# --- endpoints ---

@app.get("/status", dependencies=[Depends(verify_api_key)])
def status():
    proc = find_account_process(MAIN_SCRIPT_MATCH, ACCOUNT)
    account = connector.account_info()
    open_position = get_open_position()

    return {
        "account": ACCOUNT,
        "main_process": {"running": proc is not None, "pid": proc["pid"] if proc else None},
        "bot_state": infer_bot_state(position_open=open_position is not None),
        "execution_mode": config.execution.mode,
        "kill_switch_active": kill_switch.is_active(),
        "account_info": {
            "balance": account.balance,
            "equity": account.equity,
            "profit": account.profit,
            "currency": account.currency,
        },
        "open_position": open_position,
        "recent_closed_trades": get_recent_closed_trades(limit=5),
    }


@app.post("/stop", dependencies=[Depends(verify_api_key)])
def stop():
    kill_switch.activate(reason="Stopped via API")
    return {"ok": True, "message": "Kill switch activated — main.py will halt gracefully on its next check."}


@app.post("/start", dependencies=[Depends(verify_api_key)])
def start():
    was_active = kill_switch.is_active()
    if was_active:
        kill_switch.deactivate()

    proc = find_account_process(MAIN_SCRIPT_MATCH, ACCOUNT)
    launched_pid = None
    if proc is not None:
        logger.info("main.py --account %s already running (pid=%s), skipping launch.", ACCOUNT, proc["pid"])
    else:
        launched_pid = launch_python_script(MAIN_SCRIPT, PROJECT_ROOT, extra_args=["--account", ACCOUNT])

    return {
        "ok": True,
        "account": ACCOUNT,
        "kill_switch_was_active": was_active,
        "main_process_was_already_running": proc is not None,
        "launched_pid": launched_pid,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")