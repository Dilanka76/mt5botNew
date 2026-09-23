# live2 — the live strategy, as it runs

**As of 2026-09-23.** This describes the real-money account exactly as the
two bots are configured and running today. Anything not written here is
not in the strategy.

---

## The account

| | |
|---|---|
| Broker / login | BlackBull Markets Live, **529340** |
| Symbol | **XAUUSDp** — spread about **$0.12** |
| Costs | **$6.00 per lot**, charged in full when a trade **opens** |
| Money in | $271.50 deposited (15 Sep), $50.00 withdrawn (23 Sep) → **$221.50 net** |
| Balance | **$226.28** |
| Bots | **live2_m3** (magic 950003) and **live2_m5** (magic 950005), one MT5 account |
| Terminal | its own MT5 install; Algo Trading must be **green** or every order is refused |

Both legs trade the same account at the same time. They do not consult each
other: they can hold opposite positions, and that is allowed.

---

## The rule

### Entry

EMA13 crosses EMA21 **and the candle closes on the new side**. The bot enters
at the open of the next candle, within about 2 seconds.

There is **no other entry condition**. No gap rule, no ADX, no tick volume, no
candle colour, no trend filter, no time-of-day filter. 26 such filters have
been tested on real trades and every one lost money.

### Exit — whichever comes first

1. **Take-profit** — a fixed distance from the entry price, placed at the
   broker with the order.
2. **The opposite cross** — when a candle closes with the EMAs crossed back,
   the bot closes the position and **immediately opens the other way**.
   *This is the real stop-loss.* It exits when the trend actually turns
   rather than at a fixed distance.
3. **The broker backstop** — a real stop order held at the broker, for a news
   spike or for the case where the bot itself dies. It has never fired.

### The target is chosen by the M15 trend

At entry, the bot reads EMA13 vs EMA21 on the **M15** chart:

- running **with** that trend → the bigger target
- running **against** it → the normal target

Decided once, at entry, and never changed afterwards.

---

## The two legs

| | **live2_m3** | **live2_m5** |
|---|---|---|
| Chart | M3 | M5 |
| Engine | `dual_cross_confirmed_swap` | `dual_cross_confirmed_swap_adx` |
| Target — with the M15 trend | **$8** | **$10** |
| Target — against it | **$6** | **$8** |
| Software stop-loss | **none** | **none** |
| Broker backstop | **$30** | **$35** |
| Breakeven / trailing / runner | none | none |
| Daily loss limit | none | none |

Targets and stops are **price distances, per ounce**. One lot is 100 ounces,
so at 0.03 lots a $6 target is worth about $18.

### Position size, by balance

The lot size is recalculated from the live balance **before every order**.

| live2_m3 | | live2_m5 | |
|---|---|---|---|
| up to $50 | 0.01 | up to $200 | 0.01 |
| up to $100 | 0.02 | up to $500 | 0.02 |
| up to $200 | 0.03 | up to $1,000 | 0.04 |
| up to $300 | 0.04 | above $1,000 | 0.08 |
| up to $1,000 | 0.06 | | |
| above $1,000 | 0.12 | | |

**At today's $226: M3 trades 0.03 lots, M5 trades 0.02.** The ladder steps
down by itself as the balance falls, which is the main brake on a losing run.

---

## Hours

- Trading window **04:00 – 01:29 Colombo**, every trading day.
- **Flat for the weekend at 20:00 UTC on Friday** — any open position is closed.

---

## Protections built into the bot

| | |
|---|---|
| **Warm-up** | no entry for one full candle after a restart, so a restart cannot fire on a cross that closed while the bot was down |
| **Stale cross** | a cross measured against a candle more than one candle old is never entered (added after an order refusal left one pending for hours) |
| **One attempt per setup** | a refused order cannot fire late once trading resumes |
| **Manual trades rejected** | anything opened by hand in MT5 is closed on sight |
| **Self-heal** | a position the bot has forgotten is adopted and managed again |
| **Close before forget** | the broker close happens first; the bot only forgets a position once the close succeeded |

---

## What live2 does **not** do

- No software stop-loss — tested at every size from $5 to $15, all lost money
- No breakeven, no trailing stop, no profit runner
- No daily loss limit
- **No entry filter is acting**

The **range filter is loaded in record-only mode**: on every entry it writes
`in_range` plus the ceiling and floor it saw, and changes nothing. demo2 is
the account actually skipping range entries. live2 will only follow if the
forward evidence passes the rule fixed on 2026-09-21 — range trades must
lose money in both halves of the test period, with at least 20 of them.

---

## What the numbers say so far

- **133 trades** since 16 September, **45% won**, profit factor **1.00**
- Average win **$26.41**, average loss **$21.64**
- Every loss leaves through the opposite cross; every take-profit is a win
- Worst drawdown: **−$388 (74%)** from the peak, on 22 September
- Deepest any trade has gone against us: about **40% of the way** to the backstop

**In one line:** trade every confirmed EMA13/21 cross on gold, take a fixed
profit, reverse when the trend turns, let the broker backstop catch a
disaster — and let the position size step down by itself as the balance falls.
