# mt5tradingbot

Rule-based EMA-cross scalping bot for XAU/USD on MetaTrader 5. No ML — every
decision comes from an explicit, deterministic EMA5/13/21 cross strategy.
See `config/settings.<account>.yaml` for the exact thresholds.

Runs multi-account: one process per account (e.g. `demo1`, `live1`), each
with its own MT5 terminal instance, config, and logs. **See [SETUP.md](SETUP.md)
for the full multi-account setup walkthrough.**

## Layout

```
config/settings.<account>.yaml   per-account: symbol, EMA periods, gap/TP
                            thresholds, sessions, position sizing tiers,
                            execution mode. config/settings.demo1.example.yaml
                            shows the expected format.
.env.<account>              per-account MT5 login secrets + API key
                            (gitignored). .env.demo1.example shows the
                            expected format.
bot/config.py               loads the above (given --account) into typed
                            config objects
bot/mt5_connector.py        connect/disconnect, account + symbol lookups
bot/data/market_data.py     pulls OHLC candles into pandas DataFrames
bot/indicators/ema.py       EMA5/13/21 calculation
bot/strategy/cross_detector.py   EMA13/21 cross detection (candle-close
                            confirmed) + gap calculation
bot/risk/                   position sizing (fixed lot table) — TODO
bot/execution/              sends orders to MT5 (gated by execution.mode) —
                            needs updating for this strategy (TP-only orders,
                            bot-driven close on opposite cross)
bot/kill_switch.py          `touch KILL_SWITCH_<account>` halts that
                            account's trading immediately
bot/logging_setup/          logs/<account>/app.log (human-readable) +
                            decisions.jsonl (one line per evaluation:
                            taken/skipped + why)
bot/process_utils.py        account-aware duplicate-instance detection,
                            shared by main.py/watchdog.py/api_server.py
main.py                     entry point — run with --account <name>
scripts/watchdog.py         hang detection for one account's main.py —
                            run with --account <name>
scripts/daily_report.py     writes reports/daily/<account>/<date>.md
scripts/check_crosses.py    prints historical EMA crosses for verification
                            against your own MT5 chart
```

## Setup

See [SETUP.md](SETUP.md) for the full multi-account walkthrough. Quick
version for one account:

```
pip install -r requirements.txt
cp .env.demo1.example .env.demo1
cp config/settings.demo1.example.yaml config/settings.demo1.yaml
# fill in .env.demo1 / config/settings.demo1.yaml, then:
python main.py --account demo1
```

`execution.mode` defaults to `shadow` — no real orders are ever sent until
that's changed, and even then `require_demo_account: true` refuses to trade
on anything but a confirmed demo account.

## Verify EMA + cross detection (current step)

```
python scripts/check_crosses.py --account demo1
```

Run this **on the EC2 Windows server** (MetaTrader5 only imports on
Windows). It prints every EMA13/21 cross found in the recent XAUUSD M1
history — candle time, direction, close, EMA13, EMA21, and the calculated
gap — so it can be checked by eye against the live MT5 chart before any
entry/exit logic gets built on top of it.

## Current status

- EMA calculation + cross detection: implemented, ready to verify against
  the chart.
- Session gating, EMA5-touch waiting, the one-position state machine,
  position sizing, and execution are not wired up yet — see the plan for the
  build order.
