# XAU/USD (Gold) EMA Scalping Strategy — Plain English Explanation

This is the exact strategy the trading bot follows. No AI, no guessing —
just these fixed rules, checked automatically every minute.

---

## 1. What it trades

- **Instrument:** XAU/USD (Gold), broker symbol `XAUUSDm` (Exness demo)
- **Chart:** 1-minute candles
- **Price used:** BID price (not ask) for all indicator calculations

## 2. The indicators

Three Exponential Moving Averages (EMA), all on the 1-minute chart:

- **EMA 5** — fastest, hugs price closely
- **EMA 13** — medium
- **EMA 21** — slowest, the main trend line

The whole strategy is built around watching **EMA 13 vs EMA 21**.

## 3. The signal: a "cross"

- When EMA 13 moves from *below* EMA 21 to *above* EMA 21 → this is a
  **bullish cross** (signal to look for a BUY).
- When EMA 13 moves from *above* EMA 21 to *below* EMA 21 → this is a
  **bearish cross** (signal to look for a SELL).

**Important rule:** a cross only counts once a full 1-minute candle has
**closed** with EMA 13 on the new side of EMA 21. We deliberately ignore
crosses that happen mid-candle and reverse before the candle closes — this
avoids false signals from noise.

## 4. BUY setup (step by step)

1. A bullish cross happens (EMA13 crosses above EMA21).
2. On the candle where this happens, measure the **gap**:
   `gap = closing price − EMA13` (at that same candle)
3. **If the gap is less than $5** → enter a BUY trade **immediately** at the
   market (ask price).
4. **If the gap is $5 or more** → don't enter yet. **Wait** until the price
   touches the EMA5 line, then enter BUY at market.
   - **Cancel rule:** if a bearish cross happens *while waiting*, the whole
     BUY plan is thrown away — no entry happens. Instead, the bot starts
     fresh evaluating the new bearish (SELL) signal from step 1 of the SELL
     setup below.
5. **Take Profit:** exactly **$5 above** the entry price.
6. **Exit:** if EMA13 crosses back *below* EMA21 at any point — close the
   trade **immediately**, no matter if it's winning, losing, or breakeven.

## 5. SELL setup (exact mirror of BUY)

1. A bearish cross happens (EMA13 crosses below EMA21).
2. Measure the gap: `gap = EMA13 − closing price` (mirrored formula)
3. **Gap under $5** → enter SELL immediately at market (bid price).
4. **Gap $5 or more** → wait for price to touch EMA5, then enter SELL.
   Same cancel rule: a bullish cross before the touch scraps the SELL plan.
5. **Take Profit:** exactly **$5 below** the entry price.
6. **Exit:** an opposite (bullish) cross closes the trade immediately,
   regardless of profit or loss.

## 6. No separate stop-loss

There is **no fixed stop-loss price** on any trade. The only two ways a
trade ends are:
- it hits the $5 take-profit, or
- the EMA13/21 crosses back the other way (forced exit).

This means a losing trade could stay open (and lose more than $5) if the
market keeps trending against it without the EMAs crossing back. This is
exactly as specified — flagging it here so it can be sanity-checked.

## 7. Only one trade at a time — continuous loop

The bot never has more than one open trade. Every single valid EMA13/21
cross does two things, always in this order:

1. **Close** whatever trade is currently open (if any) — instantly, no
   matter its profit/loss.
2. **Then** freshly check the new setup in the new direction (the gap
   check, maybe the EMA5 wait) — this is a brand new decision each time,
   never an automatic re-entry.

So the bot is always doing one of three things: **in a trade**, **waiting
for price to touch EMA5** after a big-gap cross, or **watching for the next
cross**.

## 8. Trading hours (Sri Lanka time, UTC+5:30)

New trades are only allowed to open during:
- **4:00 AM – 8:00 AM**
- **12:00 PM – 11:00 PM**

Outside these hours, no new trade opens — the bot just watches. If a trade
is already open when a session ends, it's left alone to hit its take-profit
or get closed by an opposite cross naturally; it is not force-closed just
because the clock ran out.

**One extra rule we had to decide ourselves** (not explicitly in the
original spec, please double-check this): if a cross happens *outside*
these hours, it is completely ignored — the bot does not "remember" it and
does not act on it once trading hours resume. It simply waits for the next
fresh cross that happens *after* a session has opened.

## 9. Position size (based on account balance, not risk %)

| Account balance | Lot size |
|---|---|
| Under $100 | 0.02 |
| $100 – $300 | 0.03 |
| $300 – $1,000 | 0.06 |
| Over $1,000 | 0.12 |

This is a fixed lookup table, checked fresh at the moment each trade opens
— it is **not** a percentage-of-balance risk calculation.

---

## Things worth double-checking with your trader friend

We had to make a few judgment calls where the original rules were slightly
ambiguous. Please confirm these are correct:

1. **The SELL gap formula** — we assumed it mirrors the BUY formula exactly
   (`EMA13 − close` instead of `close − EMA13`), so "gap" is a positive
   number when price has moved in the trade's favor, in both directions.
2. **"Touching" EMA5** — we treat this as price (bid) reaching the EMA5
   level or crossing through it, checked continuously (about once per
   second) while waiting.
3. **No stop-loss** — confirming this is intentional, since it means a
   trade's loss is only capped by how fast the EMAs cross back, not by a
   fixed dollar amount.
4. **Ignoring crosses outside trading hours** — confirming the bot should
   NOT act on a cross that happened while the session was closed, even
   after the next session opens.
