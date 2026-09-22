"""Is every account actually healthy, right now?

User, 2026-09-16, with real money trading: *"now all demo accounts and the
live account need to be checked, working without any issue"*.

Every other tool here answers one question about one thing -- is the config
loaded, what is the open trade, how did yesterday go. This answers the
question you actually ask each morning, for every account at once, and it
does it WITHOUT opening a single MT5 connection: each bot already writes
logs/<account>/status.json every heartbeat, and that file carries the whole
picture.

The check that matters most is free, because of where those two fields come
from. `bot_state` is the ENGINE's view; `open_position` is read from the
BROKER. So bot_state IDLE with a position present means the engine has
forgotten a trade it is holding -- the 2026-09-14 fault that left a live
position for 2h32m with no take-profit and nobody watching. It cost hours to
find then. It is one comparison here.

Seven checks per account:
  1. is a process running
  2. is status.json FRESH (a stale file means the loop has stopped, even
     though the process is alive -- "running" and "working" differ)
  3. kill switch
  4. session open, and the balance
  5. ORPHAN: engine flat while the broker holds a position
  6. errors in app.log recently
  7. entries REFUSED recently -- the symptom an orphan produces downstream

    python scripts/health_check.py
    python scripts/health_check.py --accounts live2_m3,live2_m5 --minutes 60

Read-only: reads files and the process list. Opens no MT5 connection.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, discover_configured_accounts, validate_account_name

PS_LIST = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
)
STALE_SECONDS = 180          # heartbeat is ~60s; three misses is a stopped loop


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default=None,
                   help="comma-separated; default is every configured account")
    p.add_argument("--minutes", type=int, default=30,
                   help="how far back to look for errors and refused entries")
    return p.parse_args()


def processes() -> list[dict]:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PS_LIST],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return [data] if isinstance(data, dict) else data


def is_running(procs: list[dict], account: str) -> bool:
    pattern = re.compile(rf"--account[=\s]+{re.escape(account)}(?:\s|$)", re.IGNORECASE)
    return any("main.py" in (p.get("CommandLine") or "").lower()
               and pattern.search(p.get("CommandLine") or "") for p in procs)


def recent_decisions(account: str, since: datetime) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines()[-4000:]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["timestamp"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            out.append(e)
    return out


# MT5 order refusals worth naming. The retcode sits on the traceback lines
# AFTER the "[ERROR]" line, so a bare error count never shows it: on
# 2026-09-21 demo2 reported "148 error(s)" all day while the real message
# -- the terminal's Algo Trading button was off -- sat unread underneath,
# and demo2 took no trades at all.
KNOWN_REFUSALS = {
    "10027": "ALGO TRADING IS OFF in this account's MT5 terminal -- every order is refused. "
             "Stop the bots, switch Algo Trading on (green), THEN start them: a restart "
             "clears the failed cross, otherwise it fires late the moment trading resumes.",
    "10026": "the BROKER'S SERVER has switched off algo trading on this account -- the "
             "terminal button cannot fix it. Check the account in its MT5 terminal (a demo "
             "account may have expired) or ask the broker.",
    "10019": "the broker refused an order for lack of money / margin",
    "10018": "the broker refused an order because the market is closed",
    "10031": "no connection to the broker's trade server",
}


def recent_refusals(account: str, minutes: int) -> list[str]:
    """Plain-language reasons for MT5 order refusals in the last `minutes`,
    read from the traceback lines that follow each error."""
    path = PROJECT_ROOT / "logs" / account / "app.log"
    if not path.is_file():
        return []
    cutoff = datetime.now() - timedelta(minutes=minutes)
    in_window, found = False, []
    for line in path.read_text(errors="ignore").splitlines()[-4000:]:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:            # continuation lines inherit the last timestamp
            in_window = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= cutoff
        if not in_window:
            continue
        for code, meaning in KNOWN_REFUSALS.items():
            if f"retcode={code}" in line and meaning not in found:
                found.append(meaning)
    return found


def recent_errors(account: str, minutes: int) -> int:
    """ERROR/CRITICAL lines in the last `minutes`, ignoring the expected
    single-instance refusal -- the main.py task retriggers every 5 minutes by
    design and each attempt logs one. Counting those would mean every account
    always looks unhealthy, which is how a real error gets skimmed past."""
    path = PROJECT_ROOT / "logs" / account / "app.log"
    if not path.is_file():
        return 0
    cutoff = datetime.now() - timedelta(minutes=minutes)
    count = 0
    for line in path.read_text(errors="ignore").splitlines()[-4000:]:
        if "[ERROR]" not in line and "[CRITICAL]" not in line:
            continue
        # Both of these are NORMAL and log loudly. The main.py task
        # retriggers every 5 minutes by design, and each attempt logs a
        # single_instance refusal; a kill-switched bot logs a CRITICAL halt
        # every time it starts. Counting either makes every account look
        # permanently unhealthy -- which is exactly how a real error gets
        # skimmed past. On 2026-09-16 this reported "7 error(s)" on four
        # accounts and every one was a kill-switch halt.
        if ("single_instance" in line
                or "already holds the single-instance" in line
                or "Kill switch is active" in line):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= cutoff:
            count += 1
    return count


def main() -> None:
    args = parse_args()
    accounts = ([validate_account_name(a.strip()) for a in args.accounts.split(",")]
                if args.accounts else discover_configured_accounts())
    procs = processes()
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=args.minutes)
    problems: list[str] = []

    print("=" * 88)
    print(f"HEALTH CHECK — {len(accounts)} account(s), looking back {args.minutes} minutes")
    print("=" * 88)

    for account in accounts:
        print(f"\n{account}")
        status_path = PROJECT_ROOT / "logs" / account / "status.json"
        running = is_running(procs, account)
        ks = (PROJECT_ROOT / f"KILL_SWITCH_{account}").exists()

        if not status_path.is_file():
            print("  no status.json — this account has never run")
            continue

        try:
            st = json.loads(status_path.read_text(errors="ignore"))
        except json.JSONDecodeError:
            print("  status.json unreadable")
            problems.append(f"{account}: status.json unreadable")
            continue

        written = datetime.fromisoformat(st["written_at_utc"])
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        age = (now - written).total_seconds()
        state = st.get("bot_state", "?")
        position = st.get("open_position")
        info = st.get("account_info") or {}
        balance = info.get("balance")

        flags: list[str] = []
        if not running and not ks:
            flags.append("NOT RUNNING and no kill switch — it should be up and is not")
            problems.append(f"{account}: not running")
        if running and age > STALE_SECONDS:
            flags.append(f"LOOP STALLED — status.json is {age / 60:.0f} min old "
                         f"while the process is alive")
            problems.append(f"{account}: loop stalled")
        # A STOPPED bot holding a position is an ABANDONED trade. The kill
        # switch makes the bot exit its loop, so the swap exit and the
        # software stop both stop happening -- only whatever the broker
        # holds is left. This used to print "OK", because not-running WITH a
        # kill switch reads as intentional. It is intentional; the open
        # position is what makes it dangerous, and that is the combination
        # nothing was checking. 2026-09-16: demo1_m5 and demo2_m5 both sat
        # like this for half an hour, reported healthy.
        if position and not running:
            flags.append(f"ABANDONED TRADE — bot stopped while holding "
                         f"{position.get('direction', '?')} {position.get('volume', '?')} "
                         f"lots. No swap exit, no software stop; only what the broker "
                         f"holds. Restart it, or close the trade by hand.")
            problems.append(f"{account}: stopped while holding a position")

        # THE ONE THAT MATTERS: engine flat, broker holding a trade.
        if position and state != "IN_POSITION" and running:
            flags.append(f"*** ORPHAN — engine says {state} but the broker holds "
                         f"{position.get('direction', '?')} {position.get('volume', '?')} "
                         f"lots. Nothing is managing that trade. ***")
            problems.append(f"{account}: ORPHANED POSITION")

        errors = recent_errors(account, args.minutes)
        blocked = [d for d in recent_decisions(account, since)
                   if d.get("action") == "entry_blocked_existing_position"]
        if errors:
            flags.append(f"{errors} error(s) in the log in the last {args.minutes} min")
            problems.append(f"{account}: {errors} recent error(s)")
            for meaning in recent_refusals(account, args.minutes):
                flags.append(f"*** {meaning} ***")
                problems.append(f"{account}: {meaning.split(' -- ')[0]}")
        if blocked:
            flags.append(f"{len(blocked)} entry(s) REFUSED — the broker held a position "
                         f"the engine had forgotten")
            problems.append(f"{account}: {len(blocked)} refused entries")

        bal = f"${balance:,.2f}" if isinstance(balance, (int, float)) else "?"
        pos = "flat"
        if position:
            pos = (f"{position.get('direction', '?')} {position.get('volume', '?')} lots "
                   f"@ {position.get('price_open', '?')}")
        print(f"  {'running' if running else 'STOPPED':<9} "
              f"{'kill switch ON' if ks else 'kill switch off':<16} "
              f"state {state:<12} {pos:<28} {bal}")
        print(f"  status.json {age:.0f}s old   session {st.get('session_status', '?')}   "
              f"mode {st.get('execution_mode', '?')}")
        for f in flags:
            print(f"    !! {f}")
        if not flags:
            print("    OK")

    print(f"\n{'=' * 88}")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("All accounts healthy.")


if __name__ == "__main__":
    main()
