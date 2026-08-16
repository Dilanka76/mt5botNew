# New `dual_cross` strategy engine — replaces `ema5_only`

## Context

Working with the account owner (via detailed rule-by-rule clarification,
partly with Gemini's help — see the pasted spec), a genuinely new strategy
design was developed: tick-based entry the instant a candle opens (near a
$0.05 provisional EMA13/21 equal point), a one-time validation check when
that candle closes, a mandatory $15 stop-loss per trade, and — the core new
idea — the bot keeps watching for an OPPOSITE-direction signal even while a
trade is already open, allowing up to 2 simultaneous opposite positions
(hard-capped), with a specific close-the-original-or-close-the-new-one rule
once the second position's own cross candle closes.

This does not patch the current live design (documented in
`docs/STRATEGY_CURRENT.md`, running on `demo1` as `strategy_variant:
gap_threshold`) — it's a different mechanism entirely (no gap-threshold
concept, no EMA5/EMA9, no single-position assumption), so per the user's own
instruction this is a clean rewrite, not an adaptation of the existing
`EMAScalpEngine`.

**Confirmed scope** (via clarifying questions):
- Build as a **new engine that REPLACES `ema5_only`** specifically —
  `EMA5OnlyEngine`/`bot/strategy/state_machine_ema5_only.py` is deleted,
  and this new engine takes its `strategy_variant` slot under a proper new
  name (`dual_cross`), not "ema5_only". The current live `gap_threshold`
  engine (`EMAScalpEngine`, `bot/strategy/state_machine.py`) is **completely
  untouched** — still running as-is on `demo1`.
- Dashboard/status layer (Flutter app, `status.json`, gateway API) —
  **deferred**, explicitly out of scope for this effort. It will only ever
  show one of two simultaneous positions during shadow/demo testing; must
  be fixed before any `live_execute` use of this variant.
- A 3rd entry signal blocked by the 2-position cap, or blocked by session
  being closed, does **not** consume that candle's one-shot-entry slot — a
  later qualifying tick in the same still-forming candle can still enter
  once the cap frees up (or session opens).
- The engine-level hedging-guard fallback (belt-and-braces check right
  before a 2nd real order, redundant with the startup abort) **skips just
  that one entry and keeps running** if it somehow fires — logs CRITICAL,
  does not crash the bot or touch the already-open first position.

**Known open risk, explicitly deferred to deployment time, not blocking
implementation**: no code in this repo currently checks whether the MT5
account is in netting or hedging margin mode. If netting, two real
simultaneous opposite positions are impossible at the broker regardless of
what this code does. This only matters for real order placement — shadow
mode and backtest are unaffected — and the plan below builds in an explicit
guard (§6) plus a mandatory verification step before ever enabling
`demo_execute`/`live_execute` for this variant (§9).

---

## 0. Save both documents first, before any code changes

Two files, committed and pushed as their own commit, before touching any
code:

1. **`docs/STRATEGY_DUAL_CROSS_SPEC.md`** — the user's full specification,
   verbatim as pasted (the "EMA Scalping Strategy — New Design (spec for
   implementation)" document). The authoritative source of truth for what's
   being built.
2. **`docs/STRATEGY_DUAL_CROSS_IMPLEMENTATION_PLAN.md`** — this
   implementation plan itself (this file's content, §0-§7 plus critical
   files and verification), saved as a permanent repo reference alongside
   the spec — not just left in the ephemeral session plan file.

Both match the precedent already set by `docs/STRATEGY_CURRENT.md` earlier
in this project. Only once both are committed does implementation (§1
onward) begin.

---

## 1. New engine file — `bot/strategy/state_machine_dual_cross.py`

Delete `bot/strategy/state_machine_ema5_only.py`. New file, new class
`DualCrossEngine`. **Does not subclass `EMAScalpEngine`** — almost every
method's internals differ (single position slot → up to 2), so inheritance
would only invite reuse of logic built for the wrong assumption. It does
import and reuse the stateless/shared pieces from `bot/strategy/
state_machine.py` and `bot/strategy/cross_detector.py`: `Direction`,
`OpenPosition` (dataclass reused as-is, see below), `is_within_session`,
`calculate_lots`, `log_decision`. It does **not** use `detect_cross`/
`calculate_gap`/`CrossEvent` — there's no gap-threshold or close-based cross
concept in this design at all; entry is purely the tick-based near-touch
rule.

### Data structures

- **`self.positions: dict[Direction, OpenPosition] = {}`** — keyed by
  direction (not a list/ticket-set): the spec guarantees at most one BUY +
  one SELL simultaneously, so this key naturally prevents same-direction
  double-entry and makes "the opposite position" trivial to look up.
- `OpenPosition` (reused from `state_machine.py`) gets two new fields via a
  small subclass or by extending it directly in the new file:
  - `cross_candle_time: pd.Timestamp` — the still-forming candle whose
    close will run this position's one-time validation.
  - `is_concurrent_entry: bool = False` — true iff another position was
    already open the instant this one was entered (this is what flags a
    position as "the new one" for the §5 race resolution).
  - `validated: bool = False` — one-shot guard so the close-candle
    validation can never fire twice for the same position.
  - The existing `invalid` field is **not used** by this engine (that
    concept belongs to the old design's EMA5/EMA9 race, which doesn't
    exist here).
- `self.prev_ema13`, `self.prev_ema21: float | None` — same role as
  today's fields of the same name: the last CLOSED candle's real EMA13/21,
  the only legitimate baseline for every tick's provisional calculation.
- `self.current_candle_time: pd.Timestamp | None` — the currently-forming
  candle's timestamp, stamped onto any position opened by `on_tick` until
  the next `on_new_candle` call advances it.
- `self._entry_fired_this_candle: bool = False` — the one-shot-per-candle
  guard, reset every `on_new_candle` call, set only on an actual `_enter()`
  success (never on a cap/session block — see confirmed scope above).
- No `PendingSetup`, no `TradeState.PENDING_ENTRY` — entry is atomic, there
  is no "waiting for touch" concept. `TradeState` stays `IDLE`/`IN_POSITION`.

### Events instead of a `last_close_reason` singleton

`last_close_reason` (a `self`-stashed string) breaks the moment two
positions can close independently within the same call. Instead,
`on_tick()` and `on_new_candle()` **return an explicit list of events**:

```python
@dataclass
class OpenedTrade:
    direction: Direction; ticket: int | None; entry_price: float
    take_profit: float; stop_loss: float
    cross_candle_time: pd.Timestamp; is_concurrent_entry: bool

@dataclass
class ClosedTrade:
    direction: Direction; ticket: int | None; entry_price: float
    exit_price: float; category: str; reason: str
```

`main.py`'s real-time loop can ignore the return value (fire-and-forget, as
today); the backtest runner consumes it directly (§7).

### `__init__(config, connector, executor)`

Raises `ValueError` at construction if `config.stop_loss_usd is None` or
`config.dual_cross is None` — §4a's $15 stop-loss and the dual-cross config
section are both mandatory for this engine, not optional/dormant like in
the old design.

### `reconcile_on_startup()`

Uses the new `executor.get_open_positions()` (§6, plural) instead of the
old singular `get_open_position()`. Adopts up to 2 magic-matched broker
positions into `self.positions[direction]` with `validated=True` (no
pre-restart candle history exists to retroactively validate against — this
position simply skips its one-time check forever, log this explicitly) and
`is_concurrent_entry=False`. Defensively logs CRITICAL and force-closes any
3rd+ matching-magic position found (should never happen; don't silently
drop it the way the old singular method would).

### `on_new_candle(df_with_emas) -> list[ClosedTrade]`

No cross detection happens here — that's tick-driven (§3 below). This
method does exactly two things:

1. **§4 one-time validation.** `last_closed = df.iloc[-2]`. For each
   position in `self.positions` where `position.cross_candle_time ==
   last_closed.name` and `not position.validated`: read that candle's real
   EMA13/21 vs. the previous candle, set `validated = True` unconditionally
   (one-shot).
   - **Matches** → log `position_validated`, keep it running. If
     `position.is_concurrent_entry` is true, this is also the moment to
     force-close whatever's in `self.positions[opposite(direction)]` at its
     current price, category `"closed_by_concurrent_validation"` (no-op if
     already empty — it may have already hit its own TP/SL first).
   - **Doesn't match** → close this position immediately, category
     `"validation_failed"`, regardless of P/L. (§5's "if invalid → close
     the NEW trade, original untouched" needs no extra code — it's just
     this same rule applied to whichever position happens to be the
     concurrent one; nothing cascades.)
2. Roll `self.prev_ema13`/`self.prev_ema21` forward from this candle's real
   values; reset `self._entry_fired_this_candle = False`; advance
   `self.current_candle_time` to the next (forming) candle.

### `on_tick(tick) -> list[OpenedTrade | ClosedTrade]`

1. **§4a stop-loss + TP, every open position, independently.** Iterate
   `list(self.positions.items())` (copy — items may be removed mid-loop).
   Each position's own $15 stop and $5 TP checked against `tick.bid`,
   using each position's own `opened_monotonic` for the existing
   `POSITION_CLOSE_GRACE_PERIOD_SECONDS` grace-period logic.
2. **§3 entry + §5 concurrent-watch + §5a cap**, gated by session:
   - Compute `provisional_ema13`/`provisional_ema21` from `tick.bid`
     blended with `self.prev_ema13`/`self.prev_ema21` (identical EMA blend
     formula to the existing `_check_early_entry`, `k = 2/(period+1)` from
     `config.ema_periods.mid`/`.slow`).
   - **Genuine-flip check**: classify `prev_ema13` vs `prev_ema21` and
     `provisional_ema13` vs `provisional_ema21` — if the relationship is
     unchanged, this is not a cross, stop here (this the entire mechanism
     that makes the tolerance check meaningful — without it, price sitting
     near an already-established relationship would spam entries).
   - **Tolerance check**: `abs(provisional_ema13 - provisional_ema21) <=
     config.dual_cross.cross_tolerance_usd` ($0.05). This tolerance check
     alone, plus the flip check above, is the entire entry condition — no
     separate gap-from-open check exists in this design.
   - Direction = whichever way EMA13 now sits relative to EMA21.
   - **Cap gate**: if `len(self.positions) >= 2`, do not call `_enter()` —
     but do NOT set `_entry_fired_this_candle` either (confirmed: a slot
     freeing up later in the same candle should still be eligible).
   - **Session gate**: `is_within_session(config.sessions["dual_cross"])`
     — same non-consuming behavior if blocked.
   - **Already-fired gate**: skip if `self._entry_fired_this_candle` is
     already true for this candle.
   - If all pass: `_enter(direction, self.current_candle_time, reason)`,
     append the `OpenedTrade`, set `_entry_fired_this_candle = True` (the
     only path that sets it).
3. `_reject_manual_positions(source="tick")` — reused unchanged from the
   existing engine (`bot/strategy/state_machine.py`) or duplicated inline;
   it already iterates `get_all_positions()` with no single-position
   assumption.
4. Update `self.state`; return accumulated events.

### `_enter(direction, cross_candle_time, reason) -> OpenPosition`

`is_concurrent = len(self.positions) == 1` (captured before insertion —
this is exactly what "is this the new trade in a race" means). Hedging
guard (§6) sits here, immediately before `executor.open_market_order`,
gated on `is_concurrent and config.execution.mode != "shadow"` — only the
*second* simultaneous order is ever at risk from a netting account. On
guard failure: log CRITICAL via `log_decision`, return without opening
(confirmed scope: skip, don't crash). Otherwise: places the order, computes
`stop_loss = entry ∓ config.stop_loss_usd`, constructs the `OpenPosition`
with the new fields set, stores at `self.positions[direction]`.

### `_close_position(direction, category, reason, exit_price) -> ClosedTrade`

Pops `self.positions[direction]`, calls `executor.close_position(ticket)`
(already ticket-scoped, no change needed there), logs, returns the event.

---

## 2. Execution layer

**`bot/execution/trade_executor.py`**: add
`get_open_positions(self) -> list` returning every magic-matched position
on the symbol (mirrors the existing `get_all_positions()`'s list shape,
just filtered by magic like `get_open_position()` already is). Leave
`get_open_position()` (singular) completely unchanged — the still-live
`gap_threshold`/`EMAScalpEngine` depends on its exact "first match"
semantics. `open_market_order`/`close_position` need no functional
changes — neither one currently checks for an existing position (confirmed
by direct code reading), which is exactly the behavior this new engine
needs; `close_position` is already ticket-scoped.

**`bot/mt5_connector.py`**: add
```python
def is_hedging_account(self) -> bool:
    return self.account_info().margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
```
Checked in two places (mirrors the existing `require_demo_account`
defense-in-depth pattern already in this codebase):
1. **`main.py`, hard startup abort** — right after the existing
   `require_demo_account` block: if `strategy_variant == "dual_cross"` and
   `execution.mode != "shadow"` and `config.dual_cross.
   require_hedging_account` and not `connector.is_hedging_account()` →
   abort before the loop ever starts.
2. **Inside `DualCrossEngine._enter()`**, as described above — the
   fallback that should be structurally unreachable given guard #1, but
   exists as defense-in-depth per the confirmed "skip and keep running"
   behavior.

Neither check runs in shadow mode or backtest.

---

## 3. Config — `bot/config.py`

New dataclass:
```python
@dataclass
class DualCrossConfig:
    cross_tolerance_usd: float           # the $0.05 rule — the entire entry condition
    max_concurrent_positions: int = 2    # §5a hard cap
    require_hedging_account: bool = True # gates real 2nd-order placement
```
`AppConfig` gets `dual_cross: DualCrossConfig | None = None`. This is
deliberately a **new, separate field** from `early_entry_threshold_usd` —
that field is optional/off-by-default and paired with the old
gap-threshold immediate-entry logic; this one is mandatory for this variant
and has different semantics (entire entry condition, not a supplement to a
gap check).

`load_config()`: parse `raw.get("dual_cross")` into `DualCrossConfig` if
present; raise `ValueError` at load time if `strategy_variant ==
"dual_cross"` and either the `dual_cross:` section or `stop_loss_usd` is
missing (fail fast, in addition to the engine constructor's own defensive
re-check). No change needed to `sessions` parsing — already a generic
`dict[str, list[SessionWindow]]`, and the existing "active variant must
have a matching sessions entry" validation already covers a new
`dual_cross:` key.

Note: `gap_threshold_usd` and `take_profit_usd` remain **required**
top-level `AppConfig` fields (no default) — any account using
`strategy_variant: dual_cross` still needs a `gap_threshold_usd:` line in
its YAML even though this engine never reads it (harmless, just present
for schema compatibility; `take_profit_usd: 5.0` IS actually used, since TP
is still $5 in the new design).

YAML shape for whichever account gets this (new section, not touching any
existing account's `gap_threshold`/`ema5_only` sessions):
```yaml
strategy_variant: dual_cross
stop_loss_usd: 15.0
take_profit_usd: 5.0
breakeven_trigger_usd: null
gap_threshold_usd: 5.0   # unused by this variant, present for schema compatibility
dual_cross:
  cross_tolerance_usd: 0.05
  max_concurrent_positions: 2
  require_hedging_account: true
sessions:
  dual_cross:
    - {start: "04:00", end: "08:00"}
    - {start: "12:00", end: "16:00"}
    - {start: "19:00", end: "23:00"}
```
Position sizing: **no change needed** — `config/settings.demo1.example.yaml`
already has the `{max_balance: 50, lots: 0.01}` tier the spec asks for.

---

## 4. `main.py`

- `STRATEGY_ENGINES` dict (line 66): remove `"ema5_only": EMA5OnlyEngine`,
  add `"dual_cross": DualCrossEngine`. Update the import accordingly
  (remove `EMA5OnlyEngine` import, add `DualCrossEngine`).
- Add the startup hedging-guard abort described in §2, right after the
  existing `require_demo_account` check (~line 117-121).
- **Before deleting `EMA5OnlyEngine`**: verify no currently-configured
  account actually has `strategy_variant: ema5_only` in its real (uncommitted)
  `config/settings.<account>.yaml` on the server — grep or ask the user to
  confirm across all 5 potential accounts (demo1, demo2, live1, live2,
  live3) before this becomes a breaking change for any of them.

---

## 5. Backtest runner — `bot/backtest/runner.py`

`STRATEGY_ENGINES` dict (line 56, a **separate** dict from `main.py`'s —
easy to miss): same swap, `"ema5_only"` → `"dual_cross"`.

The existing single-slot `current_entry: dict | None` (line 123) and the
identity-diff pattern (`prev_position is not new_position`, used at several
points in the Phase 1/Phase 2 loop) cannot represent 2 simultaneous
positions and are not reused for this variant's path through the runner.
Since `DualCrossEngine.on_tick`/`on_new_candle` return explicit
`list[OpenedTrade | ClosedTrade]` events, the runner's existing two-phase
per-candle loop structure (Phase 1: tick simulation in candle-direction
order; Phase 2: `on_new_candle` close-based evaluation) stays exactly as-is
— only the body of each phase changes, from diffing `engine.open_position`
before/after to consuming the returned event list directly:

```python
events = engine.on_tick(connector.get_tick(symbol))  # or on_new_candle(window)
for ev in events:
    if isinstance(ev, ClosedTrade):
        _record_exit(ev, candle_time)
    elif isinstance(ev, OpenedTrade):
        lots = calculate_lots(connector.balance, backtest_config.position_sizing)
        _record_entry(ev, lots, candle_time, entry_type="tick_cross_entry")
```

`current_entry` becomes `open_entries: dict[key, dict]` keyed by ticket for
real-mode runs; since shadow mode's tickets are always `None` (can't
disambiguate two simultaneously-open shadow positions by ticket alone), key
by `(direction, id(position_object))` or assign a synthetic incrementing
id in `_record_entry` instead. This removes the `assert current_entry is
not None` (line 142) and its associated failure mode entirely — it simply
doesn't apply once entries/exits are matched by key instead of by a single
global slot.

This is a genuine, worthwhile improvement over a literal "diff two dicts by
direction" implementation (which would be fragile — e.g. mispairs on a
same-direction close+reopen within one call) and is a natural consequence
of `DualCrossEngine` returning explicit events rather than requiring the
runner to reverse-engineer what happened from before/after state.

---

## 6. Testing plan

Following this project's established (uncommitted scratch, MT5-stub +
`make_config()`) testing pattern. **Unit tests** (isolated engine):

1. Baseline: single tick-cross entry → TP close.
2. `$0.05` boundary: `0.05` enters, `0.0501` doesn't; a provisional value
   merely near an *already-established* (unflipped) relationship does
   **not** enter — only a genuine flip does.
3. One-shot-per-candle: a second qualifying tick after one already fired
   this candle does not re-enter; resets cleanly next candle.
4. Validation-at-close, valid outcome: stays open past its own cross
   candle's close; a later candle reverting the relationship does **not**
   re-trigger this check (already fired once).
5. Validation-at-close, invalid outcome: force-closed exactly at that
   candle's close, regardless of current P/L (test both favorable and
   unfavorable price at that moment).
6. `$15` stop-loss: fires independent of validation; verify the validation
   loop tolerates a position that's already gone (no crash from a missing
   dict key).
7. Concurrent race, valid outcome: B (opposite of open A) enters mid-A;
   B's cross candle validates → A force-closed *now* at current price
   (not TP/SL price), B continues alone.
8. Concurrent race, invalid outcome: only B closes; A completely
   untouched, continues to its own independent exit later.
9. Hard cap: 3rd qualifying signal while 2 are open is fully ignored (no
   `_enter()`, `positions` dict unchanged); a later tick in the same
   candle succeeds once one of the 2 closes mid-candle (confirms the
   "still eligible" decision).
10. Three-window sessions: boundary ticks around all six edges; an
    already-open position keeps running (TP/SL/validation still active)
    past a session close; a blocked-by-session signal is still eligible
    once the session opens, same candle if applicable.
11. Startup reconcile: 0/1/2 pre-existing magic-matched positions adopted
    correctly (`validated=True` self-heal default); defensive handling of
    a stray 3rd.
12. Config validation: `load_config()` errors clearly on missing
    `dual_cross:` section or missing `stop_loss_usd` for this variant.
13. Hedging guard: netting vs. hedging stub `margin_mode` (add
    `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`/netting sentinel constants to the
    shared fake `MetaTrader5` stub module used across these test files);
    startup abort fires only in non-shadow mode; engine-level fallback
    specifically skip-and-continues (per confirmed scope) before only the
    *second* concurrent real order.

**Integration tests** (full `run_backtest()`):

14. Synthetic OHLC engineered to hit: clean TP exit, validation-failure
    exit, a full concurrent episode (both outcomes), and a capped-3rd-signal
    episode — assert `trades` has correct count/direction/reason/profit-sign
    and correct entry/exit **pairing per ticket** when 2 trades are open
    simultaneously (the exact regression the runner rewrite must not
    introduce).
15. Real historical XAUUSDp M1 slice (via `scripts/backtest.py`'s existing
    data-loading path, which already dispatches on `config.strategy_variant`
    generically — no hardcoded engine reference to update there) as a
    smoke/sanity pass: no exceptions, no orphaned `positions` dict entries
    at run end, shadow-forced (no real `order_send` ever attempted).

---

## 7. Deployment sequencing

1. Implement + full unit-test pass + integration-test via `run_backtest()`
   against real historical data. Review trades/day, validation-failure
   rate, concurrent-episode frequency, cap-hit frequency before touching
   any live-adjacent config.
2. Add a `dual_cross` section to a demo account's settings with
   `execution.mode: shadow` — never `demo_execute`/`live_execute` yet.
   Fully additive; no existing account file touched (per the user's own
   §10: shadow mode first).
3. Run `main.py --account <that account>` in shadow mode for a sustained
   real-market period (recommend at least 1-2 weeks, enough to observe
   several full concurrent-position episodes and multiple
   validation-failure closes) — cross-check `decisions.jsonl` to manually
   confirm every entry obeys the flip+tolerance rule, every validation
   fires exactly once and matches the real close-candle relationship, the
   cap is never exceeded, sessions are respected, and both concurrent-race
   outcomes have each been observed behaving as specified.
4. Before flipping to `demo_execute`: explicitly confirm the demo account's
   real `margin_mode` via `mt5.account_info().margin_mode` (a one-line
   script, same pattern as the `early_entry_threshold_usd` config-check
   used earlier this project). If netting, either provision a
   hedging-type demo account, or set `max_concurrent_positions: 1` for
   this account until one's available — do not discover this via a
   silently-wrong second order.
5. Flip to `demo_execute` on the confirmed-hedging demo account; verify
   real MT5 terminal state shows both tickets simultaneously when
   expected, before any `live_execute` consideration.
6. Dashboard/status layer (§ deferred above) must be addressed before any
   `live_execute` use, even though it's out of scope for this effort.

---

## Critical files

- `bot/strategy/state_machine_dual_cross.py` (new — replaces the deleted
  `bot/strategy/state_machine_ema5_only.py`)
- `bot/config.py` — new `DualCrossConfig`, `AppConfig.dual_cross` field,
  `load_config()` validation
- `bot/execution/trade_executor.py` — new `get_open_positions()`
- `bot/mt5_connector.py` — new `is_hedging_account()`
- `main.py` — `STRATEGY_ENGINES` swap, startup hedging-guard abort
- `bot/backtest/runner.py` — `STRATEGY_ENGINES` swap, event-consuming
  rewrite of the Phase 1/Phase 2 entry/exit recording
- `bot/strategy/state_machine.py` (read-only reference — source of
  `Direction`/`OpenPosition` reused, untouched otherwise)

## Verification

- `python3 -m py_compile` on every changed/new file.
- Full unit test suite (§6 items 1-13) run locally via the MT5-stub
  pattern, using the persistent scratchpad venv already set up this
  session.
- Integration tests (§6 items 14-15) run via the same venv, item 15 also
  serving as a real-data sanity pass.
- Before deployment: the explicit sequencing in §7, including the
  mandatory netting/hedging check against the real demo account before
  any non-shadow use.
