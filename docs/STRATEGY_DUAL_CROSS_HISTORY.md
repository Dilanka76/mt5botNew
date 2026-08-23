# dual_cross Strategy Family — Full History

This document records the full history of the `dual_cross` strategy family used on
`demo1_m1`/`demo1_m3` (and briefly `demo1_ce`, `live1` unaffected) — every variant tried,
why each one was built, what real/backtest data showed, and the current live status.
Reconstructed from the project's working notes; kept here as a readable project reference.

**Current live status (as of 2026-08-21): `dual_cross_confirmed_swap_adx` on both
`demo1_m1` and `demo1_m3`, confirmed live since 2026-08-21 06:11 UTC.** See the bottom
sections for the full current design and the deployment story.

---

## What happened

User brought a Gemini-assisted full spec for a fundamentally different
strategy ("EMA Scalping Strategy — New Design") and explicitly asked for a
from-scratch rewrite, not an adaptation of the existing engine — see the
open-price/EMA5-9 design notes (now superseded) for what it replaced.
Full spec: `docs/STRATEGY_DUAL_CROSS_SPEC.md`.

**`dual_cross`** (`bot/strategy/state_machine_dual_cross.py`,
`DualCrossEngine`): tick-based entry near a provisional EMA13/21 equal
point (`cross_tolerance_usd`, default $0.05), one-time validation at that
candle's own close, mandatory $15 stop-loss, up to 2 simultaneous opposite
positions (hard-capped, race-resolved). Deployed live to demo1
(`execution.mode: demo_execute`, M3 timeframe) 2026-08-16, replacing
`ema5_only`.

**Root-cause finding that drove everything after**: real trade-reason
breakdown showed 83.3% of `dual_cross`'s losses were `validation_failed` —
tick-tolerance entries whose cross never actually confirmed at candle
close. This directly motivated `cross_confirmed`.

**`cross_confirmed`** (`bot/strategy/state_machine_cross_confirmed.py`,
`CrossConfirmedEngine`): same overall design, but removes tick-based
tolerance entry entirely — enters ONLY on an already-confirmed close-based
EMA13/21 cross. Since there's no more "provisional, uncertain" gap, holds
at most ONE position at a time; a fresh confirmed opposite cross
auto-replaces (category `new_cross_confirmed`).

**`cross_confirmed_adaptive_tp`**
(`bot/strategy/state_machine_cross_confirmed_adaptive_tp.py`,
`CrossConfirmedAdaptiveTPEngine`): identical entry mechanism to
`cross_confirmed`; only the take-profit distance changes, computed per
trade as `take_profit_usd - (confirming_candle.close - confirming_candle.open)`
— literal signed value. A bullish confirming candle shrinks the TP below
base; bearish grows it above base. Floored at $0.50 (logged as
`tp_floor_applied`) since the raw formula can go zero/negative if the
candle's own move is >= take_profit_usd.

## Full 12-variant backtest comparison

All runs: XAUUSDp, demo1 historical data, 2026-05-01 to 2026-08-16,
**$640.73 starting balance** (demo1's real balance at time of testing —
these are hypothetical replays starting from that balance, NOT a
continuation of demo1's actual trading history). Position sizing tiers
unchanged throughout (steps at $50/$100/$200/$300/$1000 balance, 0.01 to
0.12 lots, compounding as simulated balance moves).

| Variant | Trades | Win rate | Total P/L |
|---|---|---|---|
| dual_cross M1 / tol 0.05 | 1600 | 26.8% | -991.30 |
| dual_cross M1 / tol 0.02 | 805 | 27.0% | -708.23 |
| dual_cross M3 / tol 0.05 (retired) | 763 | 31.8% | -725.80 |
| dual_cross M3 / tol 0.02 | 335 | 27.2% | -679.34 |
| cross_confirmed M1 / $15 SL | 3911 | 39.9% | -1131.95 |
| cross_confirmed M1 / $10 SL | 3911 | 39.8% | -1117.92 |
| cross_confirmed M1 / $5 SL | 3911 | 37.8% | -1108.74 |
| cross_confirmed_adaptive_tp M1 / $5 SL | 3911 | 40.4% | -980.68 |
| cross_confirmed M3 / $15 SL | 1292 | 56.7% | +297.59 |
| cross_confirmed M3 / $10 SL | 1292 | 54.9% | -631.00 |
| **cross_confirmed M3 / $5 SL (fixed $5 TP)** | 1292 | 48.1% | **+431.14** |
| cross_confirmed_adaptive_tp M3 / $5 SL | 1292 | 51.1% | +359.76 |

### Key conclusions from this comparison

- **M1 (1-minute) was conclusively dead for this whole design family at the time.**
  Every variant tried on M1 lost money, clustered in a narrow band regardless of what
  changed.
- **M3 (3-minute) was where any edge lived.** Best result: `cross_confirmed`, M3, $5
  stop-loss, plain fixed $5 take-profit: +$431.14.
- **$10 stop-loss on M3 was a real dip, not a smooth midpoint** between $5 and $15 —
  attributed to the backtest's synthetic tick-ordering heuristic (2-point
  low/high replay) being unusually sensitive at that exact distance, not a
  genuine edge cliff.
- **Adaptive TP was a genuine improvement on M1 and a mild regression on M3** — not
  worth pursuing further on its own.
- Tighter stop-loss ($5 vs $15) could raise total P/L while lowering win rate — not a
  contradiction: it converts some would-be recoveries into small capped losses, but
  each loss costs far less.

**Note**: none of these 12-variant numbers used real-tick replay (see the backtest
undercounting fix below) — treat the qualitative conclusions as directionally right,
not the exact magnitudes.

## Status: LIVE as of 2026-08-16 — both legs on dual_cross

Went through two intermediate states before settling here: (1) both legs
`cross_confirmed_adaptive_tp` (M1+M3), (2) M3 switched to `dual_cross`
while M1 stayed on `cross_confirmed_adaptive_tp`, (3) final: M1 also
switched to `dual_cross`, plus a new 3-window session. Both legs ran the
identical strategy/tolerance/stop-loss, differing only in timeframe and
session schedule:

- **`demo1_m1`**: `dual_cross`, timeframe M1, magic `910001`,
  `sibling_magic_numbers: [910003]`, `stop_loss_usd: 15.0`,
  `dual_cross.cross_tolerance_usd: 0.02`, `max_concurrent_positions: 2`,
  `require_hedging_account: true`, session 3 separate windows:
  `04:00–08:00`, `12:00–16:00`, `19:00–22:00`.
- **`demo1_m3`**: `dual_cross`, timeframe M3, magic `910003`,
  `sibling_magic_numbers: [910001]`, same stop-loss/tolerance/cap/hedging
  settings, session `04:00–01:29` (wrap-around: active 4am through
  1:30am).
- Both legs: `take_profit_usd: 5.0`, `execution.mode: demo_execute`,
  `reject_manual_trades: true`.
- This effectively achieved the original "run dual_cross M1 and M3 side by
  side" comparison via two pseudo-accounts on demo1's single real login
  instead of two separate MT5 accounts.

**New code required**: `ExecutionConfig` grew a
`sibling_magic_numbers: list[int]` field; every engine's
`_reject_manual_positions()` now treats a position as "ours" if its magic
matches `magic_number` OR appears in `sibling_magic_numbers` — otherwise
two of the bot's own processes on one account would force-close each
other's trades within seconds. Verified: one running MT5 terminal
instance supports two independent Python client connections logged into
the same account simultaneously.

**Supervision**: four Scheduled Tasks registered — `MT5-Bot-demo1_m1`,
`MT5-Bot-Watchdog-demo1_m1`, `MT5-Bot-demo1_m3`, `MT5-Bot-Watchdog-demo1_m3`
— boot-trigger + 5-minute-repeating-check tasks. These two accounts are
NOT wired into `api_server.py`'s gateway (no remote start/stop via the
Flutter app), but they survive a hang (watchdog restarts) and a server
reboot (boot trigger) on their own.

## MAJOR finding (2026-08-16/17): the backtest was undercounting trades — now fixed

While manually verifying one real trade (2026-08-10 08:14 SELL,
`demo1_m1`) against real MT5 candle/EMA data, found a genuine confirmed
EMA13/21 cross that produced NO trade in the backtest. Root cause: **176
of 893 real ticks that minute would have qualified for a dual_cross
entry**, but the backtest's old methodology — every candle replayed as
exactly 2 synthetic ticks (low, then high) — never sampled anything
between those two extremes. **This never affected live trading** —
`main.py` polls the real current tick every second; the 2-point
approximation only existed in the offline backtest replay path.

**Fix**: `bot/backtest/runner.py`'s `run_backtest()` gained an optional
`tick_provider` parameter — when supplied, PHASE 1 replays real
historical ticks per candle instead of the 2-point approximation.
`scripts/backtest.py` gained a `--real-ticks` flag that fetches real MT5
tick history chunked by day and wires it in.

**Real-world impact, confirmed by re-running one week with `--real-ticks`**:

| Leg | Old (2-point synthetic) | New (`--real-ticks`) |
|---|---|---|
| `demo1_m1` | 45 trades, -$133.80 | **140 trades, +$78.48** |
| `demo1_m3` | 18 trades, -$127.38 | **91 trades, -$378.40** |

3-5x more trades on both legs. Not a uniform correction — M1 flipped from
a loss to a profit, M3's loss got significantly worse. **Any backtest
number produced without `--real-ticks` should not be treated as
final/trustworthy for absolute magnitude.**

New scripts: `scripts/inspect_ticks.py` (replays dual_cross's exact
tick-based entry check against real ticks for a specific window) and
`scripts/inspect_candles.py` (raw OHLC+EMA for a time window).

## SECOND major incident (2026-08-17): a rogue old demo1 process was live and fighting the current two

User reported "so many entries," instant closes, and trades that seemed
to vanish. Root cause: **an old `main.py --account demo1` process (magic
`900002`) was still running**, alongside the two current
`demo1_m1`/`demo1_m3` processes. All three traded the same real account
and — via `reject_manual_trades` — each one force-closed whatever the
OTHER processes had just opened, since none of the three magic numbers
were in each other's `sibling_magic_numbers`. This was NOT a strategy
bug — it was three processes actively sabotaging each other's trades in
real time.

**How it happened**: the old process's Scheduled Task was already
disabled, but `api_server.py` (the gateway) can launch `main.py`
processes directly via `/apiconnect/{account}/start`, bypassing Task
Scheduler entirely, and it still recognized `demo1` as a valid,
startable account.

**Fix**: killed the rogue process, then renamed
`config/settings.demo1.yaml` →
`settings.demo1.yaml.retired-superseded-by-demo1_m1-and-demo1_m3` so
`main.py --account demo1` now fails immediately instead of silently
relaunching the conflict.

**General lesson**: whenever an account is being replaced/retired in
favor of new pseudo-accounts, disabling only the Scheduled Task is NOT
sufficient if a gateway with its own direct-launch capability is still
running — the config file itself needs to be removed/renamed too.

## THIRD variant (2026-08-18): dual_cross_confirmed_entry

New engine, `bot/strategy/state_machine_dual_cross_confirmed_entry.py`
(`CrossConfirmedEntryEngine`) — sits alongside `dual_cross` as a separate
`strategy_variant`. Two mechanisms inverted relative to `dual_cross`:
entry is ONLY on an already-confirmed candle-close EMA13/21 cross (no
tick-tolerance entry at all); closing the current position tries fast
first (tick-based, within `closing_tolerance_usd` $0.02 of a genuine
opposite flip) and falls back to the candle's confirmed close.
Single-position, auto-replacing, no position cap or hedging-account
requirement.

**Testing path**: built a throwaway `demo1_ce` pseudo-account to trial
this variant in isolation first.

**User decision, made anyway despite the backtest disagreeing**: a
`--real-ticks` backtest over a 9-day window showed
`dual_cross_confirmed_entry` underperforming `dual_cross`. User's
explicit call: switch both `demo1_m1` and `demo1_m3` to it anyway, to
observe it live regardless of the backtest. `demo1_ce` then became
redundant and was retired the same day.

### Status: LIVE as of 2026-08-18 — dual_cross_confirmed_entry on both accounts

- `demo1_m1`: `closing_tolerance_usd: 0.02`, `stop_loss_usd: 15.0`,
  `take_profit_usd: 5.0`, magic `910001`, 4 session windows.
- `demo1_m3`: same closing tolerance/stop-loss, magic `910003`, session
  `04:00–01:29`, `take_profit_usd: 6.0` (deliberate difference).
- Verified via real `Order opened` log lines, not just config inspection.

## Switched BACK to dual_cross the same day (2026-08-18, later)

User had both accounts stopped then asked to revert. A sequencing
mistake happened here (processes restarted before the revert script ran)
— caught by reading the `Bot started` log line rather than assuming
success, then corrected. **General lesson reinforced repeatedly
throughout this project: always verify the `strategy_variant=` value in
the post-restart `Bot started` line before considering any strategy
switch complete, never trust the command sequence alone.**

## FOURTH variant (2026-08-19): dual_cross_tight_exit

Built from a real-trade forensic analysis (not backtest) of
`dual_cross`'s actual losses: **96.9% of real losing trades traced back
to just two mechanisms** — `validation_failed` (71.9%, a tick-based
entry whose own candle didn't confirm it) and
`closed_by_concurrent_validation` (25.0%, a second concurrent position
getting displaced). Only 1 real loss out of 32 was a genuine stop_loss
hit unrelated to either mechanism.

**`dual_cross_tight_exit`** keeps `dual_cross`'s tick-based entry but
adds two protections:

1. **`early_exit_usd`** (deployed at **$3.00**): while a position is
   unvalidated, watched every tick — if price moves this far against it,
   close immediately at that small capped loss instead of waiting for
   the candle's close.
2. **Reversal swap**: at any point, validated or not, if a later candle's
   real close confirms a genuine opposite cross, the current position is
   closed immediately (whatever its P/L) and the new confirmed-direction
   position opens right away. Replaces `dual_cross`'s concurrent-position
   mechanism with a plain single-position swap.

Single-position design (no position cap, no hedging-account
requirement). New categories: `early_exit_unconfirmed`,
`swapped_confirmed_reversal`.

**Verified locally before shipping** (stubbed-MT5 smoke test, this
project's standard practice since the Mac dev environment can't import
the real MT5 package): 20/21 checks passed.

**Deployed 2026-08-19.** Sequencing mistake happened again during this
deploy (same pattern as before) — caught and corrected the same way.

**Corrected same day, before any real trade data existed** — the user
caught two design mistakes by walking through the logic in detail:

1. **Tick-based re-entry capped at ONE attempt per candle**, not
   unlimited — every tick-based check within one candle compares
   against the SAME fixed pre-candle EMA13/21 baseline, so retrying
   repeatedly was just re-gambling on one direction.
2. **The $15 stop-loss and the $3 `early_exit_usd` net made strictly
   mutually exclusive per position** — only the net checked while
   unvalidated, only the real $15 stop once validated.

## First real-trade report (2026-08-19, ~10 hours live): swapped_confirmed_reversal identified as the dominant problem

First real run, 26 trades since the 05:14 deploy: 38.5% win rate, -$12.65
combined. 0 rule violations — the engine behaved exactly as designed.

**Key finding: `swapped_confirmed_reversal` was THE dominant loss
driver** — 12 of 26 trades (46%), only 2 wins, -$77.73, 75.4% of all
loss $. The $3 `early_exit_usd` net worked exactly as intended
(`validation_failed` shrank to near-nothing) — but the reversal swap had
no equivalent protection: it closes "regardless of P/L" at whatever
price exists the instant an opposite cross confirms.

**Discrepancy explained and FIXED (2026-08-20)**: MT5's own reported time
differs from true UTC by a real, exact offset (measured +3h on this
account's broker). `mt5.history_deals_get()`'s query args are
interpreted in this same broker convention, not true UTC — passing
true-UTC bounds silently skewed which real deals got returned. Fixed in
`bot/analytics.py` (`mt5_utc_offset(connector, symbol)` — measures the
offset FRESH every call, not hardcoded, since broker DST rules can flip
it). `scripts/inspect_live_trades.py` and
`scripts/inspect_open_positions.py` still had the OLD unfixed behavior
as of this writing — worth patching if used for anything precise.

## SECOND, much bigger instance of the same offset bug (2026-08-20): candle/tick fetches — affected EVERY backtest ever run

While building `scripts/inspect_adx.py`, candle prices didn't match
known real trade prices for the same nominal UTC window. Root cause:
**`bot.data.market_data.get_ohlc_range()`** (which wraps
`mt5.copy_rates_range()`) had never been patched with the offset
correction.

**Why this mattered much more**: `get_ohlc_range()` is used by 16+
scripts — `scripts/backtest.py`, `scripts/inspect_candles.py`, and every
`*_analysis.py` sweep script. Critically, `bot/backtest/runner.py` feeds
these same (mislabeled) candle timestamps into `is_within_session()` —
so a candle time that's actually +3h ahead of true UTC (but labeled as
UTC) produced a Colombo wall-clock reading 3 hours ahead of the truth.
**This meant every past backtest of every session-gated strategy may
have evaluated trades against the wrong 3-hour session window.**

**Confirmed NOT a live-trading issue** — `main.py` only imports the
separate `get_ohlc()` function, never `get_ohlc_range()`.

**Fix**: the correction now lives centrally inside `get_ohlc_range()`
itself — measures the offset once per call, shifts the query window
before fetching, shifts the returned index back after. Every caller is
fixed automatically. Separately fixed `scripts/backtest.py`'s
`_fetch_real_ticks()` too.

**Still not re-verified**: no past backtest number has been re-run since
this fix. **Practical takeaway**: even after this fix, no backtest here
can be "100% accurate" — real-tick replay is still an approximation of
true market microstructure, and a ~$105 real-vs-backtest divergence was
observed on this exact strategy family even before this bug was known
about. Treat every backtest as directionally informative only; real
demo-account results remain the actual verdict.

## SIXTH variant (2026-08-20, backtest-only): dual_cross_tight_exit_swap_confirm_adx — the ADX swap gate

`scripts/inspect_adx.py` (Wilder's ADX(14)) checked real ADX readings at
all 4 real swap-firing moments across two chart-verified choppy windows:
**all 4 were below 25** (11.1, 12.8, 11.7, 22.7) — the 2-candle debounce
filters fast single-candle noise but not a slower, sustained ranging
market, which is exactly what low ADX measures. Honest caveat: ADX is a
lagging indicator — one of the 4 examples had a strong 28 reading at its
ORIGINAL entry and still lost once the trend faded before the swap fired
18 minutes later. This is why the filter gates the SWAP decision only,
not fresh entries.

**`dual_cross_tight_exit_swap_confirm_adx`** — identical to
`dual_cross_tight_exit_swap_confirm` except a 2-candle-confirmed swap
also requires ADX(14) >= threshold (default 25.0) at the confirming
candle to actually fire. Below that: the swap is blocked (category
`swap_blocked_low_adx`), pending reversal cleared, held position keeps
running. NaN ADX (insufficient warmup) fails safe — also blocks.

New shared `bot/indicators/adx.py` and `SwapAdxFilterConfig` in
`bot/config.py`. Registered backtest-only, never in `main.py`. Verified
locally, 16/16 checks passing.

## SEVENTH variant (2026-08-20, backtest-only, then FINALIZED): dual_cross_confirmed_swap_adx

Built on top of the sixth variant, per explicit user instruction to drop
tick-based entry entirely. Worth noting: the request was based on a
stale finding (an early small sample showed `tick_cross` at 0% win rate,
but a later larger sample showed it as the BEST entry type) — flagged
explicitly, but the user confirmed the decision anyway.

**`dual_cross_confirmed_swap_adx`** — genuinely new, simpler engine:

- **Only entry path**: a genuine already-closed-candle EMA13/21 cross.
  No tick-based tolerance entry exists at all.
- **No $3 early-exit net, no "unvalidated" state** — every position
  opens already validated (direct structural consequence of dropping
  tick entry).
- **Real trade-off**: the $15 stop-loss now applies to every position
  from the instant it opens (previously the $3 net covered the first,
  most-dangerous window).
- Keeps unchanged: the 2-candle swap-confirm debounce, the ADX(14) >=
  25.0 gate on the swap, $5 take-profit.

**FINALIZED same day** — added the original "day one" $5 gap +
EMA5-pullback rule (from `dual_cross_tight_exit_gap_ema5`) on top of
this design, to the FLAT entry path only: gap = |close - ema13| at the
confirming candle; gap < $5 enters immediately, gap >= $5 creates a
pending setup that only fires when a later tick touches EMA5. A genuine
opposite confirmed cross while pending cancels it outright.

**Explicitly does NOT apply to the swap's re-entry** — the swap already
carries its own "is this real" confirmation (2 candles + ADX); adding a
pullback-wait on top could leave the bot flat with no position right
after closing the old one, missing the very reversal the swap exists to
catch.

Verified locally, 17/17 checks passing, including regression checks
proving the swap+ADX path is completely unaffected by the gap rule.

## Status: LIVE as of 2026-08-21 06:11 UTC — dual_cross_confirmed_swap_adx on both demo1_m1 and demo1_m3

User explicitly chose to skip the backtest step and deploy straight to
demo to observe real results. Registered in `main.py`'s
`STRATEGY_ENGINES`/`CONCURRENT_POSITION_VARIANTS` for the first time —
previously backtest-only. Migration script:
`scripts/switch_m1_m3_to_confirmed_swap_adx.py`.

**Important correction — a real, extended sequencing failure**: the
config file was updated correctly on 2026-08-20, but the running
processes were never actually killed/restarted at that time. This went
undetected through an entire evening, a full night, and a next-morning
real-trade report (40 trades, all still the OLD engine) — caught only
because `tick_cross`/`validation_failed` entries kept appearing, which
are structurally impossible in the new engine. Fixed by finding the
real PIDs, killing them directly, and manually relaunching — confirmed
via a genuinely new `Bot started` line (2026-08-21 06:11:46 demo1_m1 /
06:11:53 demo1_m3).

**General lesson**: when confirming a strategy switch went live, insist
on seeing an actual NEW `Bot started` timestamp newer than the last
command run, not just "it's running" — and if a report still shows
old-engine-only categories after a supposed switch, that alone proves
the restart didn't happen.

**True starting point for real-trade analysis of this variant: 2026-08-21
06:12 UTC onward.**

## CRITICAL INCIDENT (2026-08-21, same day): main.py never computed the 'adx' column — crashed the live loop and silently disabled the stop-loss

Caught when checking open positions: a real `demo1_m1` SELL (entry
4558.90) was sitting at broker price 4581.37 — **$7.47 past where the
$15 stop-loss should have already closed it**, floating loss -$134.82.
The position was not being managed at all.

**Root cause**: the ADX column computation had been wired into
`scripts/backtest.py` when building the ADX-gated variants, but was
never added to `main.py`'s own live loop. The engine's
`on_new_candle()` reads an `adx` column whenever a position is held
(the swap-check branch) — this raised `KeyError: 'adx'` every time.

**Why it was so damaging**: `main.py`'s loop only advances its
candle-time tracker AFTER `on_new_candle()` succeeds. Since it never
succeeded, the same candle kept getting retried every loop iteration,
crashing every time — and `on_tick()` (where the stop-loss check lives,
called right after `on_new_candle()` in the same block) never ran
again. The bot was silently frozen from managing that position from the
moment the first post-entry candle closed, onward.

**Fix**: `main.py` now computes the `adx` column identically to how
`scripts/backtest.py` already does, conditional on
`config.swap_adx_filter` being set. Both accounts needed an urgent
restart after this landed.

**General lesson**: any new dataframe column an engine depends on must
be wired into BOTH `scripts/backtest.py` and `main.py` before the
engine is considered live-ready — the backtest path working correctly
gives false confidence the live path is fine too; they're separate code
paths that don't automatically stay in sync.

**Follow-up structural fix, same day**: asked directly "can this
situation happen again?" — the answer was yes, for a more general
reason than the missing `adx` column. `main.py`'s loop only advanced
its candle-time tracker after `on_new_candle()` succeeded, and
`on_new_candle()`/`on_tick()` shared one `try:` block, so ANY future
exception in `on_new_candle()` — for any reason, on any strategy
variant — would silently disable the stop-loss check the same way,
indefinitely, with the process still showing as running. Fixed:
`on_new_candle()` is now wrapped in its own try/except; a failure there
is logged and retried next iteration, but `on_tick()` (stop-loss/TP/swap
checks) always runs regardless. Applies to every account run via
`main.py`, including `live1`.

## Candidate fixes for swapped_confirmed_reversal — consolidated master list, status as of 2026-08-21

This reconciles two separate rounds of ideas discussed across the project (an
original 3-item list, then a later 5-item list after the first fix alone wasn't
enough) into one deduplicated list — items 4 and 7 below were repeated across
both rounds under slightly different numbering.

```
1. 2-candle persistence/debounce (require reversal to persist 2 candles, not 1)
   DONE — built FIRST, as dual_cross_tight_exit_swap_confirm (deployed
   2026-08-19/20). Still part of the CURRENT live strategy
   (dual_cross_confirmed_swap_adx kept this unchanged).

2. ADX trend-strength filter (gate the swap on ADX >= 25)
   DONE — built AFTER the 2-candle debounce, because real data showed the
   debounce alone wasn't enough (still 8/8 losses). Live right now as part
   of dual_cross_confirmed_swap_adx.

3. Minimum EMA13/21 separation on the confirming candle
   NOT built. Require the gap between EMA13/21 to exceed some minimum $
   distance on the confirming candle, not just "crossed" — filters lines
   merely hugging the equilibrium point, standard whipsaw-avoidance
   technique.

4. Swap-churn circuit breaker (pause after 2-3 swaps in 30-60 min)
   NOT built. Track how many swaps fire within a short rolling window; if
   it crosses a threshold, pause new entries for a cooldown period.
   Reactive to evidence of a bad regime rather than trying to predict
   one — complementary to any signal filter, not a replacement.

5. Higher-timeframe confirmation (M15 must agree)
   NOT built. Only allow the swap if a higher timeframe (e.g. M15)
   EMA13/21 relationship agrees with the new direction. More work (new
   timeframe data feed) but a genuinely different signal than ADX or the
   EMA-gap idea.

6. Close-and-flatten instead of close-and-reverse
   NOT built. When the 2-candle confirmation fires, close and go flat
   instead of immediately reversing into a new position — doesn't fix
   signal accuracy, but removes the cost of being wrong on the reversal
   itself.

7. Volatility/ATR range filter
   NOT built — separate, older idea, on hold (failed an out-of-sample
   test in an earlier unrelated experiment, see the volatility filter
   experiment notes). Lower priority, proceed with caution if ever
   revisited. ADX (#2) is a different tool from this — measures
   directional persistence, not raw volatility magnitude — treated as a
   fresh attempt, not a retry of this failed one.
```

**2 of 7 are done and live right now** (the 2-candle debounce, and ADX). The
other 5 are still open ideas, not built.

## FIFTH variant (2026-08-19/20): dual_cross_tight_exit_swap_confirm ("the swap flip fix")

*(Numbered fifth chronologically, though described after the sixth/seventh above since
those built directly on top of it.)* Identical to `dual_cross_tight_exit`
except the reversal swap now requires TWO consecutive candles to agree
before executing.

**Real bug caught by the local smoke test before shipping**: an early
draft required the SECOND confirming candle to also be a fresh EMA13/21
flip vs the candle before it — structurally almost impossible, so it
silently cancelled every pending reversal instead of ever confirming
one. Fixed to check "does this candle's real state still oppose the
held position" instead of "did this candle itself just flip."

**Backtest result (2026-08-19 06:32 to 20:32 UTC, real ticks)**: combined
+$21.84 better than plain `dual_cross_tight_exit` — but split unevenly:
helps demo1_m3 a lot, hurts demo1_m1. Two variants showed this same
M3-benefits/M1-hurts asymmetry for confirmation-style protections.

**Critical caveat surfaced and never fully resolved**: this backtest
produced a wildly different number for the CURRENTLY-RUNNING strategy
than what actually happened live — backtest said `dual_cross_tight_exit`
should have made +$41.22 on demo1_m1 for a window where real trades
showed -$63.54. A ~$105 swing on the same strategy, same period. User
chose to deploy live anyway, to be judged on real results, not backtest.

**Deployed 2026-08-20** — confirmed live and clean on the first restart
attempt, `Bot started` at 2026-08-19 20:47:07 UTC.

## EIGHTH variant (2026-08-22, backtest-only): dual_cross_confirmed_swap_adx_entrygate

Same as `dual_cross_confirmed_swap_adx` except ADX also gates fresh
entries from flat, not just the swap. Built and locally smoke-tested
(12/12 checks), registered only in `bot/backtest/runner.py`, never in
`main.py`. Two backtest runs were attempted (2026-08-21 04:00 to
2026-08-22 00:00, both accounts) but were run while the market was
closed, which corrupted the underlying data fetch (see the market-closed
offset limitation below) — those results are unreliable and were never
re-run. Superseded by the ninth variant below, which folds in the same
"ADX gates entries" idea plus an M15 filter and a redesigned reversal
mechanism.

## A third MT5 time-offset limitation found (2026-08-22, not yet fixed): market-closed queries produce a nonsense offset

`mt5_utc_offset()` measures the MT5-vs-true-UTC offset by comparing the
*latest tick's time* to true "now." This breaks when the market is
closed (weekend, or any stale-tick period) — the latest tick can be many
hours old, producing a garbage offset (an ~11–13 hour gap was observed,
instead of the normal +3h) that corrupts any historical query run while
the market is closed. Confirmed via `scripts/check_mt5_time.py`.

**Not fixed at the source** — the function still doesn't detect or
refuse a stale-tick situation. Worked around with a new script,
`scripts/inspect_candles_fixed_offset.py`, which takes `--offset-hours`
as an explicit CLI argument instead of measuring it live, so historical
data from a day when the market was confirmed open can still be reliably
queried even while the market is currently closed. This became the
primary tool for a long, detailed real-chop-window investigation
(candle-by-candle EMA13/21/ADX tracing) that directly informed the ninth
variant's design below.

## NINTH variant built and DEPLOYED 2026-08-22/23: dual_cross_confirmed_adx_m15 — ADX+M15 entry gate, swap removed and replaced by close-and-flatten

Built after an extensive, fully collaborative real-data design session
that manually traced the critical-incident 4558.90 SELL (see above) and
a hypothetical 7-swap whipsaw chain that would have followed it if its
entry had been allowed through. Two deliberate design changes from
`dual_cross_confirmed_swap_adx`, both weighed against real traced
numbers rather than assumed as free wins:

**1. Every entry (not just the swap) is now gated by BOTH ADX(14) >=
threshold (default 25.0) AND a higher timeframe (M15)'s own EMA13/21
relationship agreeing with the signal direction.** Checked once, on the
confirming candle, before even the $5 gap/EMA5-pullback rule runs.
Either check failing blocks the whole signal outright — no pending
setup, no retry — the bot just waits for the next independent fresh
cross. This filter also blocks some real wins (one real example: a SELL
with ADX 18.70 that actually won +$29.64 under the current live
strategy) in exchange for blocking real losses; the net effect on full
real trading was not assumed going in — it's judged on live/demo
results, same as the previous variant was.

**2. The swap is removed entirely, replaced by close-and-flatten.** The
moment a single candle confirms the opposite direction from a held
position — no 2-candle wait, no ADX check on this specific decision —
the position closes immediately (whatever the P/L) and the bot goes
flat. Getting back into the market, in either direction, now requires
passing the exact same entry gate as any fresh signal (item 1 above) —
there is no special "swap re-entry" path left at all. Traced on the real
4558.90 example: one small contained exit (-$45.06), then flat through
the rest of a 64-minute chop (every remaining cross blocked by the
ADX+M15 gate), then one clean win at the next real signal (+$29.64) —
net -$15.42, meaningfully better than both the frozen real incident
(-$166.14) and a traced hypothetical fully-ungated swap whipsaw
(-$140.16) through the same window.

Unchanged: no tick-based entry (confirmed-close-only), the $5
gap/EMA5-pullback rule on flat entries (now gated by ADX+M15 first), no
$3 net, $5 take-profit.

**Stop-loss tightened to $10** (was $15) — a further explicit user
decision, applied via `scripts/switch_m1_m3_to_confirmed_adx_m15.py`.
Since `stop_loss_usd` is a shared top-level config field, not
variant-specific, a future revert to the previous variant needs to
manually restore it to $15 — the revert script doesn't touch that
field.

**M15 data plumbing**: `main.py`'s loop calls
`engine.update_m15_data(m15_df_with_emas)` once per iteration, detected
via `hasattr` so every other engine's loop is completely unaffected —
no extra fetch, no added risk for them. The M15 data is fetched the same
way the primary timeframe is (`get_ohlc` + `compute_emas`), and the
engine stores only the latest CLOSED M15 candle's ABOVE/BELOW state; if
no M15 update has arrived yet, entries fail-safe block (same pattern as
a NaN ADX value). Whether this should use M15's last closed candle
(current choice) or its still-forming current candle is an open question
left for future revisiting.

**Deliberately not registered in `bot/backtest/runner.py` yet** —
`scripts/backtest.py` has no M15-fetch wiring at all, and registering
without it would recreate the exact "registered without its required
data wired in" bug class that caused the critical incident above. If a
backtest is ever wanted, the identical M15-fetch step needs adding to
`scripts/backtest.py` first.

Locally smoke-tested (stubbed MT5, 7 scenarios, 19/19 checks passed)
before deployment: the no-M15-data fail-safe block, ADX+M15-both-agree
entries firing, either check alone blocking, the gap/EMA5-pullback path
still working once the gate passes, close-and-flatten firing on a single
opposite candle with no wait and no auto-reopen, and re-entry after
flattening still requiring the full gate.

Deployed via `scripts/switch_m1_m3_to_confirmed_adx_m15.py` to both
`demo1_m1` and `demo1_m3`, to be judged on live/demo results over the
following days — explicit user decision, same pattern as every
backtest-skipped deploy in this project's history.
