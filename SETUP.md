# Multi-account setup

This bot runs one account per process. All 5 accounts (2 demo, 3 live) run
from the same codebase, each fully independent: its own MT5 terminal
instance, `.env.<account>` secrets, `config/settings.<account>.yaml`,
`logs/<account>/`, and `KILL_SWITCH_<account>`.

## Two separate naming schemes — do not conflate them

There are two independent names in play, and they are allowed to look
different:

1. **The account identifier** — used for `--account`, `.env.<account>`,
   `config/settings.<account>.yaml`, `logs/<account>/`, and
   `KILL_SWITCH_<account>`. Always exactly one of these five clean names,
   no extra letters, no spaces:
   `demo1`, `demo2`, `live1`, `live2`, `live3`.
2. **The MT5 terminal folder path on disk** — goes inside
   `MT5_TERMINAL_PATH=` in that account's `.env.<account>` file. These
   folders carry a letter suffix purely so they're visually distinguishable
   in File Explorer; the suffix is NOT derived from, and does not need to
   match, the account identifier:

   | account | MT5_TERMINAL_PATH |
   |---|---|
   | demo1 | `C:\MT5BOTSCRIPT\MT5-Demo1 T\terminal64.exe` |
   | demo2 | `C:\MT5BOTSCRIPT\MT5-Demo2 D\terminal64.exe` |
   | live1 | `C:\MT5BOTSCRIPT\MT5-Live1 T\terminal64.exe` |
   | live2 | `C:\MT5BOTSCRIPT\MT5-Live2 D\terminal64.exe` |
   | live3 | `C:\MT5BOTSCRIPT\MT5-Live3 C\terminal64.exe` |

## 1. One MT5 terminal instance per account

Install a separate MT5 terminal instance per account (not just a separate
profile in one install) so all 5 can be logged in and trading at the same
time, at the paths in the table above.

## 2. Per-account env + config files

For each account name (`demo1`, `demo2`, `live1`, `live2`, `live3`), copy
the example files and fill them in:

```
cp .env.demo1.example .env.demo1
cp config/settings.demo1.example.yaml config/settings.demo1.yaml
```

Repeat for each account, e.g. `.env.demo2`, `config/settings.demo2.yaml`,
`.env.live1`, `config/settings.live1.yaml`, and so on.

- `.env.<account>` — `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
  `MT5_TERMINAL_PATH` (the exact letter-suffixed path for THIS account from
  the table above), `API_KEY`.
- `config/settings.<account>.yaml` — symbol, EMA periods, gap/TP
  thresholds, sessions, position sizing, execution mode. Each account's
  copy is independent, so lot sizing or session windows can differ per
  account (e.g. smaller lots on live accounts, or a different symbol).

Both file types are gitignored — never commit real credentials. Only the
`*.example` files are tracked.

If you already had a working `.env` / `config/settings.yaml` from before
multi-account support, copy their values into the new per-account files —
they're no longer read directly by any script.

## 3. Running a single account manually (for testing)

```
pip install -r requirements.txt
python main.py --account demo1
```

This connects to `demo1`'s MT5 terminal (per `.env.demo1`), loads
`config/settings.demo1.yaml`, and logs to `logs/demo1/app.log` +
`logs/demo1/decisions.jsonl`. Halt it with `Ctrl+C`, or by creating
`KILL_SWITCH_demo1` in the project root.

Running a second account alongside it is just another process:

```
python main.py --account live1
```

`main.py` refuses to start if another `main.py --account demo1` is already
running, but a `main.py --account demo1` and a `main.py --account live1`
run side by side without conflict.

## 4. Watchdog (optional, per account)

```
python scripts\watchdog.py --account demo1
```

Watches `demo1`'s `main.py` process only (matched by `--account demo1` on
its command line) and its own `logs/demo1/watchdog.log`. Run one
`watchdog.py --account <account>` per account you want hang-detection on,
same duplicate-instance rules as `main.py`.

## 5. Control API — unified gateway (optional, ONE process for all accounts)

Unlike `main.py`/`watchdog.py`, `api_server.py` runs as a **single process
for all 5 accounts**, not one per account — so a Flutter (or any) mobile
app only needs one public URL/tunnel to control every account.

```
cp .env.gateway.example .env.gateway   # fill in one master API_KEY
python api_server.py --port 8000
```

It discovers which accounts to serve by scanning for
`config/settings.<account>.yaml` files at startup — so it automatically
picks up whichever accounts you've configured so far (just `demo1`
today; more as you add them). **Adding a new account's config later
requires restarting this process** to pick it up.

Every request needs `X-API-Key: <the value from .env.gateway>` — this is
ONE shared master key for all accounts (not the per-account `API_KEY` in
`.env.<account>`, which this no longer uses). Routes:

```
GET  /accounts              list which accounts are currently served
GET  /{account}/status      e.g. GET /demo1/status
POST /{account}/start       e.g. POST /live1/start
POST /{account}/stop        e.g. POST /live1/stop
POST /stop-all              stop EVERY configured account in one call
```

An unconfigured-but-validly-named account (e.g. `demo2` before its
`.env.demo2`/`config/settings.demo2.yaml` exist) returns `404`; a
malformed account name returns `400` — it's never used to construct a
file path from unvalidated input.

**Why this process never opens its own MT5 connection**: the
`MetaTrader5` Python package only supports one live terminal connection
per process, so a single gateway process cannot hold live connections to
5 different terminals — and an earlier per-account version of this file,
which *did* hold its own MT5 connection alongside that account's
`main.py`'s independent connection to the same terminal, intermittently
hung the whole server after its first request. Instead, `main.py` writes
a small `logs/<account>/status.json` snapshot every heartbeat (60s), and
the gateway just reads that file — `/status` tells you how stale that
data is (`status_file.age_seconds` / `status_file.stale`) so the app can
show "may be out of date" rather than confidently displaying dead numbers
if a bot has actually frozen.

## 6. Daily report / cross-check scripts

```
python scripts\daily_report.py --account demo1
python scripts\check_crosses.py --account demo1
```

`daily_report.py` writes to `reports/daily/<account>/<date>.md`.

## 7. Health check — all 5 accounts at a glance

```
python scripts\health_check.py
```

Prints one table covering all 5 accounts: whether `main.py` and
`watchdog.py` are running, and how old each one's last heartbeat is —
useful before relying on the Control API gateway, and doesn't need it
running. Exit code 0 if all healthy, 1 if any aren't (usable in a script).

## 8. Task Scheduler (production)

Give each account its own pair of Scheduled Tasks (main + watchdog), each
with `--account <name>` in the task's arguments, e.g.:

- Task "MT5-Bot-demo1" → `python main.py --account demo1`
- Task "MT5-Bot-Watchdog-demo1" → `python scripts\watchdog.py --account demo1`

Repeat for `demo2`, `live1`, `live2`, `live3`. Since duplicate-instance
detection is per-account, these 10 tasks (5 accounts × main + watchdog) can
all run concurrently without stepping on each other.

If you're running the Control API gateway (Section 5), it only needs
**one** additional Scheduled Task total (not one per account) —
e.g. "MT5-Gateway" → `python api_server.py --port 8000`.