# EMA Scalping Strategy — New Design (spec for implementation)

Developed 2026-08-15 through detailed rule-by-rule clarification with the
account owner. This **replaces** `STRATEGY_CURRENT.md` and the logic in
`bot/strategy/state_machine.py` entirely — it is a new design, not a patch
to the existing one. Every rule below was confirmed explicitly, including
edge cases, before being written down here.

---

## 1. Instrument & timeframe

- Symbol: `XAUUSDp`
- Timeframe: M1 (1-minute candles)
- Indicators: EMA13, EMA21, both on candle close price
- Take-profit: fixed $5.00 (price distance from entry)

---

## 2. Signal definition

A cross is a change in which EMA is above the other:

- **BUY signal**: EMA13 crosses from *below* EMA21 to *above* EMA21
- **SELL signal**: EMA13 crosses from *above* EMA21 to *below* EMA21

The **cross candle** is the candle whose close would confirm this
transition — i.e. the previous candle's closed EMA13/EMA21 had the old
relationship, and this candle's closed EMA13/EMA21 have the new one.

---

## 3. Entry — tick-based, from the moment the candle opens

Standard EMA values are only official on candle close. To get an entry
price close to the cross candle's open (the whole point — see §6 for why
this matters), the system computes a **provisional** EMA13/EMA21 on every
live tick, starting the instant a new candle opens:

```
provisional_EMA13 = blend(current tick price, previous CLOSED candle's confirmed EMA13)
provisional_EMA21 = blend(current tick price, previous CLOSED candle's confirmed EMA21)
```

(Same EMA blending formula as normal, just fed the live tick price instead
of a close price, and always anchored to the previous candle's real,
already-confirmed EMA values — never to this candle's own not-yet-final
values.)

**On every tick, for every open candle:**
1. Recompute provisional_EMA13 and provisional_EMA21 as above.
2. If `|provisional_EMA13 - provisional_EMA21| <= $0.05` **and** the
   relationship has flipped versus the previous closed candle (i.e. a
   provisional cross) → **enter immediately** at this tick's price, in the
   direction implied by the new relationship (EMA13 now above → BUY,
   EMA13 now below → SELL).
3. This check runs continuously, every tick, for as long as the candle is
   open and no entry has fired yet for this candle's cross.

The $0.05 tolerance exists because live tick prices move in discrete
steps and may never land on the exact mathematical crossing point —
zero tolerance risks silently skipping a real cross between two ticks.

---

## 4. Validation at candle close

When the cross candle actually closes:

1. Compute the real, final EMA13/EMA21 for that candle (standard
   close-based calculation, not the provisional tick version).
2. Compare to the previous candle's EMA13/EMA21.
3. **Still shows the same cross relationship that triggered entry** → the
   trade is valid, keep it running normally (subject to TP and to §5's
   opposite-cross logic).
4. **Does NOT show the same cross relationship** (it reverted before
   close) → **close that trade immediately**, right at this instant,
   regardless of current P/L. This is not optional and does not wait for
   any other condition.

This validation check happens exactly once, at the close of the specific
candle that triggered that specific trade's entry. It does not repeat on
later candles for a trade that already passed its own validation.

---

## 4a. Stop-loss (hard backstop, always active)

Independent of everything else in this document, **every open trade also
has a fixed $15.00 stop-loss**, checked continuously on every tick, the
entire time that trade is open — from the moment it's entered until it
closes for any reason.

If price moves $15.00 against the entry price → **close that trade
immediately**, regardless of what §3, §4, or §5 are doing at that moment.

This exists specifically to bound worst-case loss on any single trade: a
trade that passes its one-time §4 validation check has no further
protection against a slow, grinding adverse move that never produces a
clean new opposite cross (§5) and never reaches TP — without this
stop-loss, such a trade could theoretically stay open indefinitely,
accumulating unbounded loss. The stop-loss guarantees a hard ceiling.

**Applies per trade, independently.** If two opposite positions are open
at once (§5's concurrent-trade scenario), each one has its own
independent $15 stop-loss, checked continuously, exactly as each one has
its own independent $5 take-profit.

---

## 5. Concurrent opposite-direction trades (core mechanism)

This is the most important and least conventional part of this design.
**The system does not stop watching for new crosses just because a trade
is already open.**

1. While any trade is open and running (has not yet hit its own $5 TP,
   and has not yet been closed by its own §4 validation), the system
   **simultaneously** continues running the exact same tick-based
   provisional check from §3 — but now watching for a cross in the
   **opposite** direction of the currently open trade.
2. The instant a new opposite-direction provisional cross is detected
   (same $0.05 tolerance rule) → **enter that new trade immediately**,
   even though the original trade is still open. At this moment there can
   be **two opposite positions open simultaneously** (e.g. one open BUY
   and one newly-opened SELL).
3. Both open trades' $5 take-profits **and** $15 stop-losses continue to
   be checked independently and continuously the entire time — **whichever
   hits TP or SL first closes on its own**, regardless of what's happening
   with the other trade or with any pending validation. Neither exit is
   ever paused or deferred by any of the logic below.
4. When the **new** trade's own cross candle closes (same §4 validation,
   applied to the new trade):
   - **New cross confirmed valid** → close the **original** trade right
     now, at whatever P/L it currently has (win, loss, or breakeven —
     doesn't matter, close it as-is) → the new trade continues running
     on its own from here.
   - **New cross invalid/reverted** → close the **new** trade immediately
     (per §4's normal rule) → the original trade is completely
     unaffected and continues running exactly as it was.
5. This entire mechanism (§5 steps 1-4) applies recursively at every
   point in time — i.e. once the "new" trade becomes the sole open trade
   (either because the original closed via step 4, or because TP closed
   one side first), the system immediately resumes watching for the next
   opposite cross against whatever trade is now open, and the same
   process repeats indefinitely.

### Worked example

- 10:00 — BUY opens (from an earlier confirmed cross), entry $4340.
- 10:04 — while BUY is still open (no TP yet, no validation issue), a new
  candle starts forming. Mid-candle, tick-based check shows EMA13 crossing
  below EMA21 → SELL trade opens immediately at $4341. **Now both BUY and
  SELL are open at the same time.**
- 10:05 — the SELL's cross candle closes.
  - **If valid**: BUY closes now at its current price (say $4342, so
    BUY made +$2 so far, well under its own $5 TP — closes anyway,
    doesn't wait for TP since the original trade is being closed by the
    new-valid-cross rule, not by hitting TP). SELL keeps running.
  - **If invalid**: SELL closes immediately. BUY keeps running,
    completely unaffected, still watching for TP or the next opposite
    cross.

---

## 5a. Maximum concurrent positions: hard cap at 2

To bound worst-case simultaneous risk during a genuinely choppy period
(where EMA13/EMA21 flip for real, back and forth, multiple times in quick
succession — each flip on its own is correctly detected per §3, nothing
is missed, but §5 as described above has no upper limit on how many
positions could stack up as a result), the following hard cap applies:

- **At most 2 positions may be open at the same time, per account.**
- While 2 positions are already open, **any further opposite-cross signal
  is ignored completely** — no new entry is attempted, even if §3's
  tick-based provisional check would otherwise fire.
- Once one of the 2 open positions closes (by TP, by SL, or by §4's
  validation-invalid rule) — bringing the open count back down to 1 —
  the system resumes normal watching for opposite-cross signals per §5,
  and may open a second position again if/when one occurs.
- This cap does not change any other rule: TP, SL, and validation all
  still apply exactly as described in §3, §4, §4a, and §5 to whichever
  positions are actually open at any given time. It only prevents a
  third (or later) position from ever being opened while 2 already are.

Worst-case simultaneous exposure is therefore bounded: 2 positions ×
$15 stop-loss each = $30 maximum combined risk at any single moment,
regardless of how many additional cross signals occur while already at
the 2-position cap.

---

## 6. Why entry timing matters this much (context, not a rule)

The account owner's stated reasoning for the aggressive early-tick entry
(§3) plus the immediate-close-on-invalidation safety net (§4): waiting
for full candle-close confirmation before entering means giving up the
favorable price near that candle's open — by the time a close-based
signal is confirmed, price has typically already moved away from the
best entry point, making trades riskier / less efficient on the way to
the $5 TP. The tick-based early entry is a deliberate trade-off: better
average entry price, in exchange for needing the immediate invalidation
safety net in §4 to cut losses fast on the crosses that don't hold.

---

## 7. Position sizing

Fixed lot size by current account balance:

| Balance ≤ | Lots |
|---|---|
| $50 | 0.01 |
| $100 | 0.02 |
| $200 | 0.03 |
| $300 | 0.04 |
| $1,000 | 0.06 |
| (no upper bound) | 0.12 |

(Only change from the current live config: added the new $50-and-under
tier at 0.01 lots. Everything above $50 is unchanged.)

---

## 8. Sessions

Sri Lanka time (Asia/Colombo, fixed UTC+5:30, no DST). No **new** entries
(neither original nor opposite-cross re-entries) outside these windows.
An already-open trade is left to run to its own natural exit (TP or §4
invalidation close) even if a session window ends while it's open.

```
04:00 – 08:00
12:00 – 16:00
19:00 – 23:00
```

This replaces the current two-window schedule
(`04:00–08:00` + `12:00–23:00`) with three narrower windows.

---

## 9. Explicitly out of scope / unchanged

- `breakeven_trigger_usd` — not part of this design; leave as
  `null`/unused, same as the current live config. (`stop_loss_usd` is
  now used — see §4a — with the fixed value $15.00.)
- No change requested to the EMA5/EMA9 indicators or any of their old
  role in the previous design — this new design does not use EMA5 or
  EMA9 at all. Only EMA13 and EMA21 matter now.
- No change to magic number, order comment, execution mode, or any other
  field not explicitly mentioned above.

---

## 10. Implementation note

This is a substantial rewrite of the entry/exit state machine, not an
incremental tweak. In particular:

- The state machine needs to support **up to two simultaneously open
  positions per account** (currently it assumes exactly zero or one),
  with a hard cap enforced at 2 per §5a — any further opposite-cross
  signal while at the cap must be ignored entirely, not queued or
  deferred.
- The tick handler needs to run the provisional-EMA check on *every*
  tick regardless of whether a position is already open, and be able to
  distinguish "watching for a fresh entry" vs. "watching for an opposite
  cross against an existing position" vs. "watching for this specific
  trade's own validation at its cross candle's close."
- Please implement this from scratch against this specification rather
  than trying to adapt the existing `§2b/§2c/§3b` logic in
  `bot/strategy/state_machine.py` — the mechanisms are different enough
  that patching the old code is likely to be more error-prone than a
  clean rewrite guided by this document.
- This should go through `shadow` mode testing first (log signals only,
  no real orders) before being enabled on `demo_execute`, given how much
  the mechanism has changed.
