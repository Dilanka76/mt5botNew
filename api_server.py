"""Unified FastAPI control/monitoring gateway for ALL 5 MT5 trading
accounts — ONE process, ONE port, for a Flutter mobile app to reach every
account through a single public URL/Cloudflare tunnel instead of one per
account.

Run with:
    python api_server.py --port 8000
or:
    uvicorn api_server:app --host 0.0.0.0 --port 8000 [--reload]

Routes are account-scoped by URL path parameter, all under an
/apiconnect prefix (matches the Cloudflare Tunnel path rule this is
deployed behind — see SETUP.md; Cloudflare forwards the full path,
including the prefix, to the origin, so the app must define routes
with it rather than expecting the tunnel to strip it):
    GET  /apiconnect/accounts             list configured account names
    GET  /apiconnect/{account}/status     that account's status (see below)
    POST /apiconnect/{account}/start      launch that account's main.py if not running
    POST /apiconnect/{account}/stop       activate that account's kill switch
    POST /apiconnect/stop-all             activate EVERY configured account's kill
                                           switch in one call (master "all off")
    POST /apiconnect/start-all            start EVERY configured account in one call
                                           (master "all on") — no per-account
                                           confirmation; see note on the endpoint
    GET  /apiconnect/{account}/analytics  daily/hourly P/L breakdown + win
                                           rate, computed live from that
                                           account's local trade ledger
    GET  /apiconnect/{account}/backtest   most recent historical backtest
                                           report (see scripts/backtest.py)
                                           for that account, by session
                                           window — a separate, much larger
                                           simulated sample, not live data
    GET  /apiconnect/{account}/analytics/full
                                           overall stats, close categories,
                                           entry types, rule-compliance
                                           violations, swap-mechanism stats
                                           (see scripts/generate_analytics_json.py)
    GET  /apiconnect/comparison           today's demo1-vs-demo2 trade-by-
                                           trade comparison, not account-
                                           scoped (see
                                           scripts/generate_comparison_json.py)
    GET  /apiconnect/content/strategy-overview
                                           hand-authored spec of the current,
                                           finalized strategy (per-engine
                                           entry/swap/breakeven rules,
                                           per-account parameters)
    GET  /apiconnect/content/research-log hand-authored chronological log of
                                           real investigations/decisions
                                           behind the strategy
    GET  /apiconnect/dashboard-v2/...     the Flutter Web dashboard itself
                                           (static files) -- MUST live
                                           under /apiconnect, since that's
                                           the only path the Cloudflare
                                           Tunnel forwards to this origin

Which accounts are served is discovered at startup by scanning
config/settings.<account>.yaml (bot.config.discover_configured_accounts())
— adding a new account's config later requires restarting this process to
pick it up.

CRITICAL DESIGN CONSTRAINT — this process makes ZERO MetaTrader5 calls,
ever, and imports nothing that imports the MetaTrader5 package. The
MetaTrader5 Python package exposes mt5.initialize()/account_info()/etc as
functions on ONE process-global connection, not an instantiable object —
confirmed via MQL5's own docs/forum. That means a single process
literally cannot hold live connections to 5 different terminals at once,
and an earlier per-account version of this file (which DID hold its own
MT5 connection, alongside that account's main.py's own independent
connection to the same terminal) intermittently hung after its first
request — very plausibly exactly this contention, with a blocking mt5.*
call inside a request handler wedging the whole single-worker server.

The fix: main.py (which already holds the one healthy connection to its
own account's terminal) periodically writes a small status snapshot to
logs/<account>/status.json (see bot/status_writer.py); this gateway only
ever reads that file. /start and /stop never needed MT5 either (pure
process-launch / kill-switch-file operations) — see SETUP.md. /analytics
follows the same pattern one step further: main.py appends every closed
trade to a local ledger (logs/<account>/trade_history.jsonl, see
bot/trade_ledger.py) as it happens, and this gateway computes the
daily/hourly breakdown live from that ledger (bot/trade_stats.py) — never
querying MT5's own trade history, so the ledger is also this bot's
permanent record, independent of whatever history depth MT5 itself
happens to retain.

SECURITY: every endpoint requires the X-API-Key header to match the
single master API_KEY in .env.gateway (NOT any per-account .env.<account>
— see SETUP.md for why one shared key is the right call for a single-user
system like this). That is the ONLY protection right now. Before exposing
this port to the internet (vs. just the local machine/LAN), you still
need HTTPS (e.g. a reverse proxy / Cloudflare tunnel) and a firewall rule
limiting who can reach the port — do not point this at the internet as-is.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles

from bot.config import AppConfig, discover_configured_accounts, load_config, validate_account_name
from bot.kill_switch import KillSwitch
from bot.process_utils import find_account_process, launch_python_script
from bot.status_writer import STATUS_STALE_THRESHOLD_SECONDS, read_status, status_file_path
from bot.trade_ledger import trade_ledger_path
from bot.trade_stats import COLOMBO, compute_daily_breakdown, compute_hourly_breakdown, compute_session_breakdown, read_trade_ledger

PROJECT_ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
MAIN_SCRIPT_MATCH = "main.py"  # substring matched (case-insensitive) against process command lines

load_dotenv(PROJECT_ROOT / ".env.gateway")
API_KEY = os.getenv("API_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bot.api_server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configs: dict[str, AppConfig] = {}
    kill_switches: dict[str, KillSwitch] = {}
    skipped: list[str] = []

    for account in discover_configured_accounts():
        try:
            cfg = load_config(account)
        except FileNotFoundError as e:
            logger.warning("Account '%s' has a settings file but is not fully configured, skipping: %s", account, e)
            skipped.append(account)
            continue
        configs[account] = cfg
        kill_switches[account] = KillSwitch(cfg.kill_switch, account)

    app.state.configs = configs
    app.state.kill_switches = kill_switches
    logger.info(
        "Gateway serving %d account(s): %s%s",
        len(configs), sorted(configs), f" — skipped (incomplete): {skipped}" if skipped else "",
    )
    yield


app = FastAPI(title="MT5 Bot Control Gateway", lifespan=lifespan)


@app.middleware("http")
async def no_cache_dashboard(request, call_next):
    """Cloudflare's default "cache static file extensions" behavior was
    caching main.dart.js at the edge (confirmed via a
    real CF-Cache-Status: HIT after a rebuild) -- Flutter's web build keeps
    that filename stable across builds, so the CDN never noticed new
    content and kept serving a stale build indefinitely. There's no
    Cloudflare dashboard access on this project to fix it with a Cache
    Rule, so instead: tell Cloudflare (and browsers) not to cache anything
    under /apiconnect/dashboard at all via the origin response headers,
    which Cloudflare's standard cache level respects unless a Page/Cache
    Rule overrides it. This dashboard's content changes often (config
    values, the research log), so "never cache" is the right call here,
    not just a fix for this one incident.

    NOTE: since this header only prevents FUTURE caching and can't evict
    something already cached, and there's no Cloudflare dashboard access
    on this project to force a purge, the mount itself was also moved from
    /apiconnect/dashboard to /apiconnect/dashboard-v2 on 2026-08-31 to
    dodge the already-stale cached copy entirely -- see the mount below.
    """
    response = await call_next(request)
    if request.url.path.startswith("/apiconnect/dashboard"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


# All routes live under /apiconnect — required because the Cloudflare
# Tunnel this is deployed behind forwards the FULL request path (including
# the "/apiconnect" prefix it matched on) to this origin, rather than
# stripping it. See SETUP.md.
router = APIRouter(prefix="/apiconnect")


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY is not configured on the server (.env.gateway)")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


def get_account_config(account: str, _: None = Depends(verify_api_key)) -> AppConfig:
    """Auth (verify_api_key) is a dependency of THIS function rather than
    listed alongside it on each route, so it's guaranteed to run first —
    an unauthenticated caller never learns whether an account name is
    valid-but-unconfigured (404) vs. malformed (400)."""
    try:
        validate_account_name(account)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid account name: {account!r}")

    config = app.state.configs.get(account)
    if config is None:
        raise HTTPException(status_code=404, detail=f"Account '{account}' is not configured on this server")
    return config


# --- endpoints ---

@router.get("/accounts", dependencies=[Depends(verify_api_key)])
def list_accounts():
    return {"accounts": sorted(app.state.configs)}


@router.get("/{account}/status")
def status(config: AppConfig = Depends(get_account_config)):
    account = config.account
    proc = find_account_process(MAIN_SCRIPT_MATCH, account)
    kill_switch = app.state.kill_switches[account]
    payload, age_seconds = read_status(status_file_path(config.logging.log_dir, account))

    return {
        "account": account,
        "main_process": {"running": proc is not None, "pid": proc["pid"] if proc else None},
        "execution_mode": config.execution.mode,
        "kill_switch_active": kill_switch.is_active(),  # live check, not status.json's (up to 60s stale) copy
        "status_file": {
            "found": payload is not None,
            "age_seconds": age_seconds,
            "stale": age_seconds is None or age_seconds > STATUS_STALE_THRESHOLD_SECONDS,
            "written_at_utc": payload.get("written_at_utc") if payload else None,
        },
        "bot_state": payload.get("bot_state") if payload else None,
        "session_status": payload.get("session_status") if payload else None,
        "account_info": payload.get("account_info") if payload else None,
        "tick": payload.get("tick") if payload else None,
        "open_position": payload.get("open_position") if payload else None,
        "recent_closed_trades": payload.get("recent_closed_trades", []) if payload else [],
    }


@router.get("/{account}/analytics")
def analytics(config: AppConfig = Depends(get_account_config)):
    """Daily/hourly P/L breakdown + win rate, computed live from this
    account's local trade ledger (never MT5) — see module docstring."""
    account = config.account
    trades = read_trade_ledger(trade_ledger_path(config.logging.log_dir, account))
    today = datetime.now(COLOMBO).date()

    # compute_daily_breakdown's loop always ends on `today` (i=0 last), so
    # its final bucket IS today's stats — reuse it rather than re-deriving
    # "today's trades" with a second, easy-to-get-wrong timezone comparison
    # (close_time is stored in UTC; today is a Colombo-local date).
    daily_breakdown = compute_daily_breakdown(trades, days=30, today=today)

    return {
        "account": account,
        "total_trades_recorded": len(trades),
        "today": daily_breakdown[-1],
        "daily_breakdown": daily_breakdown,
        "hourly_breakdown_today": compute_hourly_breakdown(trades, today),
        "session_breakdown": compute_session_breakdown(trades, config.sessions[config.strategy_variant]),
    }


@router.get("/{account}/backtest")
def backtest(config: AppConfig = Depends(get_account_config)):
    """Most recent historical backtest report for this account (see
    scripts/backtest.py — run manually on the server, not on-demand here:
    a multi-month replay takes real minutes, far too slow for a request).
    This gateway makes ZERO MT5 calls (see module docstring) — it only
    ever reads the .json summary the script already wrote to disk, never
    computes anything itself.

    Reports live at reports/backtest/<account>/<from>_<to>.json — several
    can accumulate over time from different runs, so "most recent" means
    most recently generated (file mtime), not the widest date range.
    """
    account = config.account
    out_dir = PROJECT_ROOT / "reports" / "backtest" / account
    reports = list(out_dir.glob("*.json")) if out_dir.is_dir() else []
    if not reports:
        return {"account": account, "available": False}

    latest = max(reports, key=lambda p: p.stat().st_mtime)
    data = json.loads(latest.read_text())
    return {"account": account, "available": True, **data}


@router.get("/{account}/analytics/full")
def analytics_full(config: AppConfig = Depends(get_account_config)):
    """Overall stats, close-reason categories, entry types, rule-compliance
    violations, and (for the ADX-gated variant) the swap-mechanism
    breakdown -- see scripts/generate_analytics_json.py, which writes this
    file periodically via a Scheduled Task. Same read-only-file pattern as
    /backtest above -- this gateway never computes it itself.

    Reports live at reports/analytics/<account>/latest.json.
    """
    account = config.account
    path = PROJECT_ROOT / "reports" / "analytics" / account / "latest.json"
    if not path.is_file():
        return {"account": account, "available": False}

    data = json.loads(path.read_text())
    return {"available": True, **data}


@router.get("/comparison", dependencies=[Depends(verify_api_key)])
def comparison():
    """Today's demo1-vs-demo2 trade-by-trade comparison (matched trades,
    trades only one side took, and why) -- see
    scripts/generate_comparison_json.py, written periodically via a
    Scheduled Task. Not account-scoped (spans both account pairs), so this
    route lives outside the {account}/... family. Same read-only-file
    pattern as /backtest and /analytics/full.

    Reports live at reports/analytics/comparison/latest.json.
    """
    path = PROJECT_ROOT / "reports" / "analytics" / "comparison" / "latest.json"
    if not path.is_file():
        return {"available": False}

    data = json.loads(path.read_text())
    return {"available": True, **data}


@router.get("/content/strategy-overview", dependencies=[Depends(verify_api_key)])
def content_strategy_overview():
    """Plain-language spec of the current, finalized strategy -- entry/swap/
    breakeven rules per engine and current per-account parameters. Hand-
    authored (not computed from trade data -- there's no script that could
    derive "why this rule exists" from raw MT5 records), so this is a single
    static file, not "latest by mtime" like the reports above. Updated by
    editing reports/content/strategy_overview.json directly and redeploying
    (git pull; no Flutter rebuild needed since the Flutter app just fetches
    this file's contents at runtime).

    Lives at reports/content/strategy_overview.json.
    """
    path = PROJECT_ROOT / "reports" / "content" / "strategy_overview.json"
    if not path.is_file():
        return {"available": False}

    data = json.loads(path.read_text())
    return {"available": True, **data}


@router.get("/content/research-log", dependencies=[Depends(verify_api_key)])
def content_research_log():
    """Chronological log of real investigations/decisions behind the
    strategy (what was tried, what the real data showed, what was decided
    and why) -- the user-facing mirror of this project's own memory-file
    discipline. Same static-file pattern as /content/strategy-overview
    above; append new entries to the JSON as real decisions happen.

    Lives at reports/content/research_log.json.
    """
    path = PROJECT_ROOT / "reports" / "content" / "research_log.json"
    if not path.is_file():
        return {"available": False}

    data = json.loads(path.read_text())
    return {"available": True, **data}


@router.post("/{account}/start")
def start(config: AppConfig = Depends(get_account_config)):
    account = config.account
    kill_switch = app.state.kill_switches[account]

    was_active = kill_switch.is_active()
    if was_active:
        kill_switch.deactivate()

    proc = find_account_process(MAIN_SCRIPT_MATCH, account)
    launched_pid = None
    if proc is not None:
        logger.info("main.py --account %s already running (pid=%s), skipping launch.", account, proc["pid"])
    else:
        launched_pid = launch_python_script(MAIN_SCRIPT, PROJECT_ROOT, extra_args=["--account", account])

    return {
        "ok": True,
        "account": account,
        "kill_switch_was_active": was_active,
        "main_process_was_already_running": proc is not None,
        "launched_pid": launched_pid,
    }


@router.post("/{account}/stop")
def stop(config: AppConfig = Depends(get_account_config)):
    account = config.account
    app.state.kill_switches[account].activate(reason="Stopped via API")
    return {"ok": True, "account": account, "message": "Kill switch activated — main.py will halt gracefully on its next check."}


@router.post("/stop-all", dependencies=[Depends(verify_api_key)])
def stop_all():
    """Master 'all off' — activates every configured account's kill
    switch in one call, independent of per-account UI state."""
    results = []
    for account, kill_switch in app.state.kill_switches.items():
        was_active = kill_switch.is_active()
        if not was_active:
            kill_switch.activate(reason="Stopped via API (stop-all)")
        results.append({"account": account, "was_already_stopped": was_active})
    return {"ok": True, "accounts": results}


@router.post("/start-all", dependencies=[Depends(verify_api_key)])
def start_all():
    """Master 'all on' — deactivates every configured account's kill
    switch and launches its main.py if not already running, in one call.

    DELIBERATE PRODUCT DECISION, not an oversight: this starts every
    account with NO per-account confirmation, including live-money
    accounts once they're set to live_execute. The caller (the mobile
    app's single master toggle) was explicitly chosen this way — see
    SETUP.md. If that ever needs to change, add confirmation in the
    client, not here."""
    results = []
    for account, kill_switch in app.state.kill_switches.items():
        was_active = kill_switch.is_active()
        if was_active:
            kill_switch.deactivate()

        proc = find_account_process(MAIN_SCRIPT_MATCH, account)
        launched_pid = None
        if proc is None:
            launched_pid = launch_python_script(MAIN_SCRIPT, PROJECT_ROOT, extra_args=["--account", account])

        results.append({
            "account": account,
            "kill_switch_was_active": was_active,
            "main_process_was_already_running": proc is not None,
            "launched_pid": launched_pid,
        })
    return {"ok": True, "accounts": results}


app.include_router(router)

# Serves the Flutter Web build (mt5botc_controll's `flutter build web`
# output) from this SAME process/port, so it's reachable through the SAME
# Cloudflare Tunnel URL as the API -- no second web server, no second
# tunnel route. MUST be mounted under /apiconnect, not at the site root --
# confirmed via SETUP.md (and a real 404 hit deploying this the first
# time): the Cloudflare Tunnel's "Public hostname path" is configured as
# exactly /apiconnect, so Cloudflare itself never forwards a request for
# any path outside that prefix to this origin at all -- it 404s before
# this server even sees the request. Path is configurable via
# WEB_DASHBOARD_DIR in .env.gateway since the Flutter app lives in a
# separate repo/checkout; defaults to a sibling `mt5bot_controll/build/web`
# directory next to this project. The Flutter build itself must ALSO be
# built with `flutter build web --base-href /apiconnect/dashboard-v2/` --
# otherwise its index.html's internal asset links (main.dart.js, CSS,
# manifest) point at the wrong path and 404 even once this mount is
# correct, since Flutter bakes the base href into the build output at
# build time, not something this server can fix at serve time. Statically
# skipped (no 500) if the directory doesn't exist yet -- lets this
# gateway keep running even before the Flutter build has been deployed.
#
# Mounted at /apiconnect/dashboard-v2, not /apiconnect/dashboard -- see the
# no_cache_dashboard middleware's docstring above for why: Cloudflare had
# already cached the old path's main.dart.js indefinitely before this
# server started sending no-store headers, and there's no dashboard access
# on this project to force-purge that stale entry. This is a fresh path
# Cloudflare has never cached, so it's a guaranteed MISS on first request.
DASHBOARD_URL_PATH = "/apiconnect/dashboard-v2"
_web_dashboard_dir = Path(os.getenv("WEB_DASHBOARD_DIR", str(PROJECT_ROOT.parent / "mt5bot_controll" / "build" / "web")))
if _web_dashboard_dir.is_dir():
    app.mount(DASHBOARD_URL_PATH, StaticFiles(directory=str(_web_dashboard_dir), html=True), name="dashboard")
    logging.info("Serving web dashboard from %s at %s", _web_dashboard_dir, DASHBOARD_URL_PATH)
else:
    logging.warning(
        "Web dashboard directory not found at %s -- %s will 404 until "
        "the Flutter web build is deployed there (or WEB_DASHBOARD_DIR is set "
        "in .env.gateway to point elsewhere).",
        _web_dashboard_dir,
        DASHBOARD_URL_PATH,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified control/monitoring gateway for all configured MT5 accounts.")
    parser.add_argument("--port", type=int, default=8000, help="Port to serve on (default: 8000).")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
