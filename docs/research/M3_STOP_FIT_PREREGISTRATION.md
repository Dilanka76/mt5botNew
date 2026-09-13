# M3 stop fit — pre-registration

Written 2026-09-13, **before the grid was run**. Nothing below may be edited
after seeing a result; a changed rule is a new pre-registration with its own
date.

## Why this run exists

Checking the deployed levels against measured candle behaviour, M3 failed two
of four conditions, both on the stop:

| condition | requirement | M3 ($7 stop) | M5 ($10 stop) |
|---|---|---|---|
| ① clear the noise floor | stop ≥ 2 × median candle range | 1.97× ($3.56) ✗ | 2.14× ($4.68) ✓ |
| ② clear the typical adverse move | stop > median adverse excursion | 1.09× ($6.40) ✗ | 1.25× ($8.00) ✓ |
| ③ target reachable | TP ≤ median favourable excursion | 0.87 ($6.89) ✓ | 1.05 ($9.52) ~ |
| ④ win rate beats break-even | observed > stop/(stop+TP) | 65% vs 53.8% ✓ | 71% vs 50.0% ✓ |

M3's $7/$6 were never grid-tested. M5 got 36 walk-forward cells; M3 got a
single cell run for comparison. This run gives M3 the same treatment.

## The question is the STOP, not the take-profit

Two reasons, both decided in advance:

1. **Nothing is wrong with the TP.** Condition ③ passes at 0.87. Raising it
   moves M3 toward M5's only weak spot.
2. **A stop change is one-directional; a TP change is not.** Widening a stop
   can only convert losers into winners — no winning trade is lost by giving
   it more room. Moving the TP changes the win rate in an unknown direction,
   which voids the 65% that condition ④ depends on.

So the 65% win rate is a **floor** under a wider stop, and condition ④ can be
evaluated against it honestly. Under a changed TP it could not be.

## Known contamination: the regime tilt

The M5 fit found that this dataset's **first half systematically favours a low
take-profit and its second half a high one** — visible in every stop row, a
regime tilt rather than noise. No first-half-only argmax escapes it.

Therefore **the script's own "Chosen on first half" line is ignored here.** It
ranks across all 36 cells, where the TP axis carries that tilt.

## The decision rule

Read the **TP = $6.00 row only** — six cells, one per stop. Holding the TP
fixed removes the contaminated axis and cuts the multiple-comparisons exposure
from 36 to 6.

A stop replaces $7.00 only if **all three** hold:

- **positive in both halves**, and
- **beats $7.00 in both halves** — not in the pooled total, which one half can
  carry alone, and
- the TP=$6 row is **monotone or flat around the winner**, i.e. its neighbours
  either side are also above $7.00's result. An isolated spike between two
  worse cells is noise, not a level.

If no stop clears all three, **$7.00 stays** and the condition ①/② failures are
recorded as an accepted, quantified weakness — not patched by hand.

$8.00 is the value the candle arithmetic points to (2.25× candle range, 1.25×
adverse excursion — M5's exact margins). It gets **no head start**: if the grid
prefers a different stop that clears the rule, the grid wins.

## Sanity conditions on the run itself

- Trade count must be within a few percent across the row. A stop change moves
  exit prices, not entries; a materially different `n` means something other
  than the stop moved.
- Top-5 stability is reported by the script over all 36 cells. Below 4 of 5,
  the whole grid is fitting noise and **nothing deploys**, regardless of what
  the TP=$6 row says.

## What this run cannot decide

- **The runner.** `bot/backtest/runner.py` does not simulate `tp_runner_*` at
  all — stage 2 returns the runner-off result for every setting. Its output is
  ignored here by construction.
- **The TP itself.** Out of scope by the reasoning above.
- **Lots, sessions, the daily loss limit.** Unchanged.

## Deployment scope if a stop wins

`demo1_m3` first. `live2_m3` follows **only after** demo1_m3 has traded the new
stop, because live2 is real money going live 2026-09-14 and a backtest result
is not a reason to change a level on a funded account mid-week.
