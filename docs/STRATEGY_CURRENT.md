# EMA Scalping Strategy — Current Live Design (as of 2026-08-15)

This document describes the **exact logic currently running live on the
`demo1` account**, verified directly against the code
(`bot/strategy/state_machine.py`, `bot/strategy/cross_detector.py`) and
against real trade logs after deployment. It supersedes
`docs/STRATEGY.md` and `docs/STRATEGY_PROPOSED_OPEN_GAP.md`, both of which
describe earlier, now-replaced designs.

---

## 1. Instrument & timeframe

- **Symbol:** XAUUSDp (gold vs. USD, broker-specific `p` suffix)
- **Timeframe:** M1 (1-minute candles)
- **Indicators:** EMA5, EMA9, EMA13, EMA21, all computed on candle close price
  - `EMA_t = price_t * k + EMA_{t-1} * (1 - k)`, `k = 2 / (period + 1)`
- **Take-profit distance:** $5.00 (fixed, price distance from entry)
- **Gap threshold:** $5.00 (see entry logic below)

---

## 2. Entry logic

A **cross** is defined as EMA13 and EMA21 changing which one is above the
other, evaluated on a fully-closed candle's own final EMA13/EMA21 values.

### 2a. Gap calculation (open-price anchored)

When a cross candle closes, the "gap" is measured using **that candle's own
OPEN price** against EMA13 — not its close price:

- BUY cross: `gap = candle.open - EMA13`
- SELL cross: `gap = EMA13 - candle.open`

This was a deliberate change from an earlier close-based formula: the
candle's open is a real, tradeable price from the moment the prior candle
handed off, whereas the close is already "stale" by the time the cross is
confirmed.

### 2b. Small gap (< $5.00): tick-based near-touch entry ONLY

**There is no close-based "immediate" entry anymore.** A small-gap cross
can *only* be entered while its own candle is still forming, via a
live tick-by-tick check:

1. On every tick, while the account is idle (no open position) or the
   current position is already marked **invalid** (see §3), and no
   wide-gap setup is pending:
2. Recompute a **provisional** EMA13/EMA21 using ONLY:
   - the current tick's real bid price, blended with
   - the **previous, already-closed** candle's real EMA13/EMA21
   - (never the current, still-forming candle's eventual close — that
     value doesn't exist yet)
3. If `|provisional_EMA13 - provisional_EMA21| <= early_entry_threshold_usd`
   (a configurable near-touch threshold, currently **$0.10** on demo1),
   the price is considered "close enough" to the equal point.
4. Direction is inferred from which side the previous closed candle's real
   EMA13/EMA21 were on (below → an upward/BUY cross is approaching; above →
   a downward/SELL cross is approaching).
5. If the resulting gap (tick price vs. provisional EMA13) is still under
   the $5.00 threshold, enter immediately at the current tick's price.

**If this tick-based check never catches it during that candle's own
formation, and the candle then closes with a confirmed small-gap cross,
that cross is simply skipped — no trade, no second chance later.** This is
intentional: the design never enters based on a candle that has already
fully closed.

With `early_entry_threshold_usd` left unset (`null`, the field's default),
**no small-gap trade can ever fire** — this makes the field a hard
dependency for the entire small-gap trade category, not just a tuning knob.

### 2c. Wide gap (≥ $5.00): wait for EMA5 touch

Unaffected by the above — this path never used a close-based fallback in
the first place:

1. On the cross candle's close, if gap ≥ $5.00, a "pending setup" is
   recorded (direction + gap), and the account waits.
2. On every subsequent tick, if price touches EMA5
   (`tick.bid <= EMA5` for a pending BUY, `tick.bid >= EMA5` for a pending
   SELL), enter at that tick's price.
3. Any fresh opposite cross (regardless of its own gap) cancels a pending
   setup outright before it can touch.

### 2d. Session gating

No new entry (either path) is allowed outside configured session windows
(Asia/Colombo local time, fixed UTC+5:30, no DST):

```
04:00–08:00
12:00–23:00
```

An already-open trade is left to run to its natural exit even if the
session closes while it's open.

---

## 3. Exit logic

### 3a. Take-profit — unconditional

Checked on every tick regardless of anything else below. Fixed $5.00
distance from entry (`BUY: entry + 5`, `SELL: entry - 5`).

### 3b. Validity watch + the exit "race"

Every candle close, the open position is re-checked: does EMA13/EMA21
still match the direction it was opened in?

- **Yes** → nothing happens, keep holding.
- **No (position becomes "invalid")** → two things now race, evaluated
  every candle/tick, whichever completes first wins:
  - **(a) EMA5 vs EMA9 confirms the reversal** (checked only once already
    invalid): if `EMA5 < EMA9` for a BUY position (or `EMA5 > EMA9` for a
    SELL), the reversal is confirmed → **close the position immediately**,
    THEN **immediately open a fresh trade in the now-confirmed opposite
    direction, at the current price** — no waiting, no near-touch check
    (mathematically, the near-touch check can only ever predict a return
    to the *original* direction from an invalid state, never a
    continuation, so it cannot serve this role).
  - **(b) A brand-new, independently confirmed WIDE-gap opposite cross**
    completes its own EMA5-touch entry (§2c) → that new trade automatically
    closes the old invalid position as part of opening.
- If EMA13/EMA21 flips back to match the *original* direction before
  either of those completes, the position simply goes back to "valid" and
  keeps running normally — no exit, a false alarm.

**Known, deliberately accepted gap:** a fresh SMALL-gap opposite cross
that coincides with a position going invalid (the common case, since both
stem from the exact same EMA13/EMA21 transition) can no longer replace it
directly — that specific close-based catch is gone, by design (§2b). The
position must wait for EMA5/EMA9 confirmation (§3b-a) instead, which then
immediately re-enters the confirmed direction anyway. Net effect: the
same trade eventually happens, just one candle later than the old design,
and priced wherever EMA5/EMA9 confirms rather than at the original cross
candle's close.

### 3c. Dormant, unused fields

`stop_loss_usd` and `breakeven_trigger_usd` remain supported in code for
backward compatibility but are both `null` (off) in the finalized design —
take-profit + the exit race above are the only exits actually in use.

---

## 4. Position sizing

Fixed lot size by current account balance (not risk-% based):

| Balance ≤ | Lots |
|---|---|
| $50 | 0.01 |
| $100 | 0.02 |
| $200 | 0.03 |
| $300 | 0.04 |
| $1,000 | 0.06 |
| (no upper bound) | 0.12 |

---

## 5. Observed real-world behavior worth knowing

On 2026-08-14, shortly after §2b/§3b were deployed, a ~1-hour choppy
stretch caused **4 consecutive rapid direction flips**, each one a loss
that grew larger than the last (−$24, −$18, −$33, −$46), before a 5th flip
caught a real move and won the $5 take-profit (+$30). That single stretch
accounted for almost the entire day's net loss.

The mechanism: during a whipsaw, each flip now costs more than under the
old close-based-immediate design, because the position must wait a full
extra candle for EMA5/EMA9 confirmation before closing and re-entering —
during which price keeps drifting further against it. The old design
replaced a position on the very same candle the fresh cross confirmed,
capping the cost of a false flip at one candle's drift. This is a real,
observed tradeoff of the current design, not a bug — worth weighing
against its stated benefit (never entering off a fully-confirmed, already-
stale candle).

---

## 6. Design history (chronological, for context)

1. **Original design:** close-based gap, opposite cross instantly closes
   the trade, no EMA5/EMA9 exit race.
2. **Open-price/EMA5-9 redesign** (docs/STRATEGY_PROPOSED_OPEN_GAP.md):
   switched gap to open-price anchored; introduced the invalid → race →
   close-or-revalidate exit model; small-gap crosses still entered
   immediately at candle close.
3. **Current design (this document):** removed close-based immediate
   entry entirely for small-gap crosses — replaced with the tick-based
   near-touch check; added automatic direct re-entry in the confirmed
   opposite direction immediately after an EMA5/EMA9-triggered close.
