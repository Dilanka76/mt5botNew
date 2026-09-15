"""Is the RUNNING bot using the config file on disk?

User, 2026-09-16, hours before funding a real account: *"according to
today's changes, demo2 migration to live2 -- the cache, or something, any
missing thing, and out of the strategy can it trade or not"*.

"Cache" is the right word. A bot reads its config ONCE, at startup, and
holds it in memory for the life of the process. Edit the YAML and the
running bot knows nothing about it. Every verification tool here reads the
FILE, so a file can be perfect while the process trading your money runs
something else entirely. The only proof a setting is live is that the
process STARTED AFTER the file was last written.

The gateway (api_server.py) caches the same way, separately, and never
notices a file change either -- it just does not place trades, so a stale
gateway misreports rather than misbehaves.

Checks per account:
  1. the config file, and when it was last written
  2. the running process, and when it started
  3. START AFTER WRITE?  the only real proof
  4. what that process itself reported loading, from its own startup line
  5. the kill switch, so "not trading" is never assumed

    python scripts/config_is_live.py --accounts live2_m3,live2_m5

Read-only: reads files and the process list. Touches no trading state.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name

PS_LIST = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Select-Object ProcessId,CommandLine,"
    "@{n='Started';e={$_.CreationDate.ToString('yyyy-MM-ddTHH:mm:ss')}} | "
    "ConvertTo-Json -Compress"
)
STARTED_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="live2_m3,live2_m5")
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


def bot_process(procs: list[dict], account: str) -> dict | None:
    pattern = re.compile(rf"--account[=\s]+{re.escape(account)}(?:\s|$)", re.IGNORECASE)
    for p in procs:
        cmd = p.get("CommandLine") or ""
        if "main.py" in cmd.lower() and pattern.search(cmd):
            return p
    return None


def last_start_line(account: str) -> tuple[datetime | None, str]:
    path = PROJECT_ROOT / "logs" / account / "app.log"
    if not path.is_file():
        return None, ""
    hit = ""
    for line in path.read_text(errors="ignore").splitlines():
        if "Bot started:" in line:
            hit = line
    if not hit:
        return None, ""
    m = STARTED_RE.match(hit)
    when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") if m else None
    return when, hit.split("Bot started:", 1)[1].strip()


def main() -> None:
    args = parse_args()
    procs = processes()
    problems: list[str] = []

    print("=" * 84)
    print("IS THE RUNNING BOT USING THE CONFIG ON DISK?")
    print("A bot reads its config once, at startup. Editing the file changes nothing")
    print("until the process restarts -- so the proof is START TIME vs WRITE TIME.")
    print("=" * 84)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        cfg = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        print(f"\n{'=' * 84}\n{account}\n{'=' * 84}")
        if not cfg.is_file():
            print(f"  config: MISSING ({cfg.name})")
            problems.append(f"{account}: no config file")
            continue
        written = datetime.fromtimestamp(cfg.stat().st_mtime).replace(microsecond=0)
        print(f"  config written   {written:%Y-%m-%d %H:%M:%S}   {cfg.name}")

        started_line, _ = last_start_line(account)
        proc = bot_process(procs, account)
        if proc is None:
            print("  process          NOT RUNNING")
            # A bot that starts, logs, and exits on its kill switch leaves no
            # process to compare -- but its startup line is still written BY
            # that process and still proves which config it read. Reporting
            # only "NOT RUNNING" made a correctly kill-switched account look
            # unverified, which is the opposite of the truth.
            if started_line is not None and started_line >= written:
                print(f"  VERDICT          config PROVEN LOADED — a process read this file at "
                      f"{started_line:%H:%M:%S}")
                print("                   and then exited (kill switch). Nothing is trading it now;")
                print("                   the configuration itself is confirmed.")
            elif started_line is not None:
                print("  VERDICT          *** UNVERIFIED *** — the last process to start read an")
                print("                   OLDER file. Start it once to confirm the current config.")
                problems.append(f"{account}: config never loaded by any process")
            else:
                print("                   Nothing has ever started on this account.")
        else:
            started_raw = (proc.get("Started") or "").replace("T", " ")
            try:
                started = datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                started = None
            print(f"  process          pid {proc['ProcessId']}, started "
                  f"{started_raw or 'unknown'}")
            if started is None:
                print("  VERDICT          UNKNOWN — could not read the process start time")
                problems.append(f"{account}: start time unreadable")
            elif started >= written:
                print(f"  VERDICT          LOADED — started {int((started - written).total_seconds())}s "
                      f"after the file was written")
            else:
                behind = int((written - started).total_seconds())
                print(f"  VERDICT          *** STALE *** — the file was written {behind}s AFTER")
                print("                   this process started, so the running bot is trading an")
                print("                   OLDER configuration than the one you just verified.")
                problems.append(f"{account}: running a stale config — restart it")

        when, spec = last_start_line(account)
        if spec:
            # The startup line can PREDATE the running process: a bot takes a
            # few seconds to connect to MT5 and write it, so running this
            # straight after `schtasks /Run` shows the PREVIOUS run's values
            # under a correct LOADED verdict. A tool built to catch stale
            # readings must not serve one -- 2026-09-16, when it displayed an
            # old lot ladder beside a freshly restarted bot.
            stale_line = (proc is not None and started is not None and when < started)
            if stale_line:
                print(f"  it reported      ({when:%Y-%m-%d %H:%M:%S})  *** THIS IS THE "
                      f"PREVIOUS RUN ***")
                print("                   The current process has not written its startup line")
                print("                   yet. Wait a few seconds and re-run; the values below")
                print("                   are NOT what is loaded now.")
            else:
                print(f"  it reported      ({when:%Y-%m-%d %H:%M:%S})")
            for chunk in re.findall(r"\S+=\S+", spec):
                key, _, value = chunk.partition("=")
                print(f"      {key:<28} {value}")
        else:
            print("  it reported      no 'Bot started' line in app.log yet")

        ks = PROJECT_ROOT / f"KILL_SWITCH_{account}"
        print(f"  kill switch      {'ON — will not trade' if ks.exists() else 'off — free to trade'}")

    gateway = next((p for p in procs if "api_server.py" in (p.get("CommandLine") or "")), None)
    print(f"\n{'=' * 84}\ngateway (api_server.py)\n{'=' * 84}")
    if gateway is None:
        print("  NOT RUNNING — the app and dashboard have no backend.")
    else:
        started_raw = (gateway.get("Started") or "").replace("T", " ")
        print(f"  pid {gateway['ProcessId']}, started {started_raw}")
        print("  It caches every account's config at ITS OWN startup, separately from the")
        print("  bots. A stale gateway MISREPORTS in the app; it never places a trade, so")
        print("  this is a display problem, not a trading one.")

    print(f"\n{'=' * 84}")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        raise SystemExit(1)
    print("Every running bot is using the config file on disk.")


if __name__ == "__main__":
    main()
