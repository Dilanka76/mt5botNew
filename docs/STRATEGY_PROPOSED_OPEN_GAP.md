# Finalized strategy design v2: open-price entry + EMA5/EMA9 exit (design confirmed — code NOT yet implemented)

**Status: DESIGN FINALIZED by the user, through extensive real-example
verification against `demo1`'s actual live data. The CODE has not been
written yet, and nothing is deployed to `demo1` or `live1`. The actual,
currently-running live strategy is still exactly as documented in
`docs/STRATEGY.md` (close-price gap, $10 stop-loss, $2 breakeven-stop)
until this is built, backtested, and rolled out. This document is the
complete reference spec for what the strategy SHOULD do once
implemented.**

This version replaces the earlier draft of this document (which only
covered the open-price gap change) — it now also covers the exit-side
redesign (removing the dollar stop-loss/breakeven, adding the
EMA5/EMA9 check) that came out of a long, careful session of the user
walking through their own real, physical trading experience and
verifying it step by step against real `demo1` cross data.

---

## Where this design comes from — mapping real trading experience to the technical rule

The user trades this manually, by eye, and built this design from what
they actually observe on a live chart. Each technical rule below exists
because of a specific real observation, not an abstract idea:

| What the user sees, trading by hand | The technical rule it becomes |
|---|---|
| "A cross looks like it's happening on the chart, but by the time the candle actually closes, it turns out to be misleading — the **open price** is what really mattered for that move" | Gap is calculated using the cross candle's own **open** price, not its close |
| "Sometimes what looked like a real cross reverses again almost immediately — that's a false signal" | The "is the cross still valid" check, re-run on every new candle close |
| "When I see that happen, I don't panic-exit instantly — I want a second, faster confirmation before I actually get out" | The EMA5/EMA9 check — a faster pair used specifically to confirm a reversal before acting on it |
| "A fixed dollar stop-loss and a fixed dollar breakeven don't match how I actually watch and manage a trade" | Both `stop_loss_usd` and `breakeven_trigger_usd` are removed entirely — take-profit and the EMA5/EMA9 logic are the only exits left |

---

## Part A — The full entry logic (unchanged from earlier verification)

**1. Indicators**: EMA5, EMA13, EMA21 on the 1-minute (M1) chart,
XAUUSDp. (EMA9 is new — see Part B.)

**2. Entry trigger — the cross**: EMA13 crosses EMA21, **confirmed only
once that candle fully closes** — never on the still-forming live
candle. This rule applies everywhere in this design, not just here.
- EMA13 crosses **above** EMA21 → bullish cross → BUY setup
- EMA13 crosses **below** EMA21 → bearish cross → SELL setup

**3. The gap check — using the cross candle's own open price**:
```
BUY:  gap = open price − EMA13
SELL: gap = EMA13 − open price
```

**Verified against a real trade** (`2026-08-13 19:30 UTC`, a BUY cross
on `demo1`'s real data, cross-checked candle-by-candle against the
user's own MT5 mobile chart until the exact candle, its open (4362.52),
close (4365.22), high (4366.45), and low (4362.47) all matched):
- Close-based gap (old formula): 4365.22 − 4358.80 = **$6.42** → would WAIT for EMA5
- Open-based gap (new formula): 4362.52 − 4358.80 = **$3.72** → enters IMMEDIATELY

The candle moved $2.70 within that one minute, which is exactly why the
two formulas disagree — and exactly the real-world pattern the user
described: the close can be misleading about what the move actually did.

**4. Gap decision rule**: under $5 → enter immediately, at the current
price. $5 or more → wait, watching every tick, for price to touch EMA5,
then enter at the touch price.

**5. Session windows**: 04:00–08:00 and 12:00–23:00 Colombo time. A
cross outside these hours is ignored completely, not banked for later.

---

## Part B — The full exit logic (this is what's new)

**6. Take-profit**: $5 distance from entry, checked every tick. This is
the **only** fixed exit left.

**7. Removed entirely**: the $10 `stop_loss_usd` and the $2
`breakeven_trigger_usd`. Both are set back to `null`/unset — the code
already supports this with zero changes, since both were always
optional.

**8. The "still valid" check** — on every new candle close, does
EMA13/21 still match the open trade's direction? This is the exact same
comparison used to detect a cross in the first place, just re-applied
continuously while a trade is open.
- Still matches → nothing happens, trade keeps running.
- No longer matches ("invalid") → step 9 begins.

**9. Two things start at once, racing each other**, the moment a trade
looks invalid:
   - **(a) A brand-new, fully independent entry evaluation begins** for
     the opposite direction — the *exact same* process as Part A above
     (its own gap check, its own immediate-or-wait decision). This can
     take just one candle (if its gap is under $5) or several (if it
     has to wait for an EMA5 touch).
   - **(b) The bot starts watching EMA5 vs EMA9** on the *original*
     trade — a new indicator pair, faster than EMA13/21, added
     specifically for this check.

**10. Whichever of these two finishes first is what closes the original
trade:**
   - If **(a)** finishes first — the new opposite trade actually opens
     for real — that closing of the old trade happens automatically,
     at the same moment, same as the bot's original always-closes-on-
     opposite-cross behavior. ("Confirmed" here means the *whole new
     entry process completed*, not just the bare EMA13/21 flip — those
     two are NOT the same moment, since the new entry can be delayed
     waiting for an EMA5 touch.)
   - If **(b)** finishes first — EMA5 and EMA9 cross against the
     original trade, confirmed on that candle's close — the original
     trade closes right there, faster than the new entry managed to
     complete. This is the case that gives EMA5/EMA9 real, distinct
     value: it's the fast path for exactly the situation where the new
     setup's own gap is wide and it's still waiting on an EMA5 touch.
   - Both checks are only ever evaluated on candle closes — never on a
     still-forming candle, same rule as everywhere else in this design.

---

## What still needs to happen before the CODE is written and this goes live

1. **Build it** — this needs a new EMA9 indicator, a new tracked state
   on open positions ("is this trade currently in the invalid/watching
   state"), and new logic checked on every candle close. This is a
   materially bigger build than anything else this project has shipped
   so far (breakeven-stop, stop-loss, lot tiers were all much smaller).
2. **Full backtest across real historical data** — replay all of
   `demo1`'s and `live1`'s actual trade history using this whole design,
   compare total win rate and P/L against the current live baseline.
   One verified example is not enough evidence on its own.
3. **In-sample / out-of-sample check** — same discipline the ATR
   volatility filter needed (looked good full-range, failed a proper
   split-sample test). Don't trust a single full-range backtest result.
4. Only if the backtest holds up: deploy to `demo1` first, run it for
   real, confirm it behaves as backtested, before ever touching `live1`.

This document is the settled design — the next step is building it as a
backtest, not writing live code directly.
