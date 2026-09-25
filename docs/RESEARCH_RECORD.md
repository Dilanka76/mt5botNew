# What this project tested, and what it found

**A complete record, closed 2026-09-25.**

This is written so that nobody — including a future version of this
project — has to repeat any of it. Every number here came from real
trades or from a simulation run against real gold prices with real costs
charged. Where a result is uncertain, it says so.

---

## 1. The short version

An automated strategy traded gold (XAUUSDp) on an MT5 account from
August 2026. It was built around an **EMA13/21 confirmed cross**: enter
when the two moving averages cross and the candle closes on the new
side, take a fixed profit, and reverse when they cross back.

On demo it made **+$1,816 in three weeks**. On that evidence, real money
was funded on 15 September 2026.

**The strategy has no edge.** Tested over a full year it loses about
**$0.22 per trade**. The three good weeks were a lucky streak. Real
money was funded on the day the streak ended.

**Money in: $271.50. Withdrawn: $50. Left at the end: $55.83. Net loss
about $166.**

Real trading was stopped on 24 September 2026 and has not resumed.

---

## 2. What was actually tested

### The original strategy, over a year

| Leg | Trades | Win rate | After commission |
|---|---|---|---|
| demo2_m3 | 3,126 | 51.5% | **−$887** |
| demo2_m5 | 2,343 | 46.4% | **−$409** |

M3 **wins more than half its trades and still loses**, because the
average loss ($16.87) is bigger than the average win ($15.63). It needs
a **52.4%** win rate to break even and delivers 51.3%.

Commission is **more than half the loss** — $482 of $887, at $6 a lot
across 3,126 trades.

### Three opposite strategy shapes, all just short of breaking even

| Shape | Win rate needed | Delivered | Short by |
|---|---|---|---|
| Trend (EMA cross) | 52.4% | 51.3% | 1.1 points |
| Mean reversion (trade the range) | 21.6% | 19.9% | 1.7 points |
| Never exit early (hold to target) | 82.3% | 82.0% | 0.3 points |

**Three structurally opposite strategies, all landing just under zero.**
That is the signature of an efficient market, not of three fixable rules.

### Four exit rules on the same entries

| Exit | Wins | Average win | Average loss | Result |
|---|---|---|---|---|
| Opposite cross (the live rule) | 50.5% | $6.51 | $6.97 | −616 $/oz |
| Range-hold | 52.4% | $6.53 | $7.45 | −436 |
| Trend-hold | 67.2% | $6.82 | $14.82 | −607 |
| Hold always | **82.0%** | $6.41 | **$29.89** | −180 |

Raising the win rate raises the average loss by exactly as much. **Win
50% of the time and lose money; win 82% of the time and lose money.**

### Timeframes

M3, M5, M15 and H1 were each tested over a year. All negative. The
slower timeframes cut commission from $510 a year to $30 — and the
per-trade edge worsened by about the same amount.

### Entry filters — 31 of them, all failed

Colour, tick volume, EMA separation, ATR bands, candle decisiveness,
gap size, time of day, sessions, the Asian box, ADX, the efficiency
ratio, EMA50/EMA100 trend, H1 parent-candle trap, EMA squeeze,
double-candle confirmation, late/extended entries, signal clustering,
loss cooldowns, cross-leg closes, flip chains, the consolidation box and
the trader-drawn range.

**The most instructive one:** the user was sure the losses came from
sideways markets. Over a full year, with one variable changed:

| | Trades | Result |
|---|---|---|
| All trades | 3,311 | −$416.73 |
| Consolidation trades removed | 3,077 | −$408.68 |

The 234 removed trades were worth **−$0.03 each**. Every other trade was
worth **−$0.13**. **The chop trades were four times better than the
rest.** The observation was real — sideways markets do produce runs of
losses — but they produce the quick wins too, and the wins are bigger.

### Five different strategy families

| Family | Verdict |
|---|---|
| Bollinger mean reversion | −$2,137 over two years |
| RSI extremes | −$1,284 over two years |
| NR7 volatility contraction | −$107 (M15), fails halves (H1) |
| Opening range breakout | Passed one year, **failed out of sample** |
| Donchian / Turtle | Passed everything, **then failed on inspection** |

---

## 3. The two that nearly got through

### Opening range breakout

Passed the bar on 13 months: +$711, both halves positive, 387 trades.
Then:

- The **year before**, which it had never seen: **+$51 over 344
  trades** — flat.
- The **New York hour** instead of London: **−$538**.

Its average win went from $17.00 to $40.55 between the two years, and
its average loss from $10.47 to $21.35 — everything roughly doubled,
because gold itself went from $2,500 to $4,300. **It is a volatility
bet, not an edge.**

### Donchian (the Turtle rule) on H4 — and why the decomposition matters

This passed every test that existed:

- 6¾ years, 323 trades, +$2,115 per ounce after real financing
- Both halves positive, including **2020–2022, a market it had never seen**
- Three timeframes (H1, H4)
- Lookbacks **10, 20, 30 and 40 all profitable in both halves** — a
  plateau, not a spike
- Beat buy-and-hold by **+$1,054**

Then the profit was taken apart:

| | Buy & hold | Donchian H4 |
|---|---|---|
| **Price movement captured** | **+$2,743.64** | **+$2,757.20** |
| Financing paid | −$1,682.73 | −$584.50 |
| Trading costs | −$0.18 | −$58.14 |
| Net | +$1,060.73 | +$2,114.60 |

**323 trades over six and three-quarter years captured $13.56 per ounce
more than buying gold once and never touching it.**

The entire outperformance is financing. This broker charges **$0.6845
per ounce per night to hold gold long** and **pays $0.2673 to hold it
short**. The strategy is short about half the time, so it paid $585
instead of $1,683.

**That is not a trading edge. It is one line in the broker's swap
table** — which the broker can change whenever it likes. And in a gold
market that does not rise, the price side produces nothing and the
financing still costs $585.

Also disqualifying on its own: the worst drawdown is **$473 per ounce at
0.01 lots**, the smallest trade that exists. A $300 account cannot trade
it at any size. It needs roughly $1,500.

---

## 4. Bugs found along the way

Each of these silently corrupted results before it was found.

| Bug | Effect |
|---|---|
| **Hand-closed trades invisible** | Closes from the phone carry magic 0; 41 scripts dropped those trades entirely and truncated partial closes |
| **Wrong-candle matching** | Analysis matched trades to the wrong candle on 97% of entries, invalidating a week of research and two deployed findings |
| **Backtest reported 0 trades for a year** | The live warm-up guard uses wall-clock time; a year replays in 90 seconds, so every entry was refused and the report printed a clean empty table |
| **Stale MT5 clock offset** | With the market closed, the offset reads the age of the last tick, shifting every reported time by hours |
| **Simulator loop off by one** | Exits on the final candle were dropped; caught by a hand-written test before any result was read |
| **Sign error on short exits** | Every simulated SELL was inverted; caught the same way |

**The pattern:** a measurement tool that answers instead of failing is
more dangerous than one that crashes. Three of these produced confident,
clean, completely wrong output.

---

## 5. The tools that came out of it

These work, and they are the part of this project worth keeping.

| Script | What it answers |
|---|---|
| `scripts/strategy_lab.py` | Runs any strategy family over years of real gold, with costs, financing, drawdown and a buy-and-hold benchmark |
| `scripts/backtest.py` + `backtest_equity.py` | Replays the live engine over a year and shows the ride, not just the total |
| `scripts/size_arithmetic.py` | Re-prices real trades at different position sizes |
| `scripts/live_review.py` | Broker truth for an account, including trades opened or closed by hand |
| `scripts/symbol_costs.py` | The real spread and swap, converted into dollars per ounce per night |
| `scripts/hold_strategy_year.py` | Any exit rule tested sequentially over a year |

Every one of them charges real costs, scores ambiguous candles against
the strategy, and prints what it cannot tell you.

---

## 6. What was learned, in order of importance

1. **Three good weeks is not evidence.** A year is the minimum, and even
   a year of one market regime can mislead.
2. **Decompose every profit.** Donchian passed six years, three
   timeframes and four parameter settings. It still wasn't an edge.
   Asking *where the money came from* is a different question from *did
   it make money*, and only the first one is worth answering.
3. **Costs decide small strategies.** $6 a lot across 3,000 trades is
   $510 a year. $0.68 a night to hold gold long is $1,683 over six
   years. Neither is a detail.
4. **The win rate and the win size trade against each other**, and the
   market prices the exchange. Every exit rule tried landed in the same
   place.
5. **Position size is not a detail either.** The live ladder grew after
   wins and shrank after losses, so the account was largest at the top
   and smallest during the recovery. Half the size would have turned the
   worst week from −$25 into +$69.
6. **An observation can be true and still not tradeable.** Sideways
   markets really do produce runs of losses. They also produce the quick
   wins, and removing them removed both.

---

## 7. The rules, if real money is ever used again

In this order, no exceptions:

1. **A full year** of history, profitable after commission **and**
   financing.
2. **Both halves** of that year positive, and a **second period it has
   never seen**.
3. **Decompose the profit.** If it is long-biased, it must beat
   buy-and-hold after financing on the price movement alone.
4. **Three weeks forward on demo**, with the rule frozen in writing
   first.
5. **A measured drawdown the account can survive** — at 0.01 lots,
   because that is the smallest trade that exists.
6. **A daily loss limit set before the first order.** The code exists
   and is switched off.

August 2026 skipped every one of these. That is the whole reason this
record exists.

---

## 8. The honest closing statement

Nothing tested here has an edge on gold. Not the original strategy, not
its opposite, not four exit rules, not 31 filters, not five published
strategy families, not four timeframes.

That is not a failure of effort — it is the answer. Simple rules on a
major instrument, paying retail commission and financing, do not make
money. The one candidate that looked real turned out to be the broker's
rate card.

The cost of finding that out was **$166 and one month**. It is normally
much more.
