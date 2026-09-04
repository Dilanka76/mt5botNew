"""What happened to the trades the entry filter BLOCKED?

The live scorecard for the colour+volume filter deployed to demo1_m3 on
2026-09-04 (see project_color_filter_demo2_validated). For every
`entry_filtered` decision, replays the real candles that followed and
works out whether that trade would have reached take-profit or
stop-loss -- i.e. whether the filter saved money or cost it.

Hypothetical entry price: the close of the confirming candle (the last
candle to close before the decision was logged), which is the price the
engine would have entered at. Take-profit and stop-loss come from the
account's own live config. The first of the two levels reached wins; a
single candle spanning both is reported as AMBIGUOUS rather than
guessed at.

IMPORTANT LIMITATION -- this is a single-trade counterfactual, not a
re-simulation of the whole account. Had the blocked trade actually been
taken, the bot would have been in a position and later signals would
have played out differently. So these figures answer "was this
individual skip right?", not "what would the account total be?".

    python scripts/check_filtered_entries_outcome.py --account demo1_m3

Read-only: connects to MT5 only to read historical candles.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

from bot.analytics import mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector

DIRECTION_RE = re.compile(r"^(BUY|SELL)\b")
USD_PER_LOT_PER_DOLLAR = 100.0  # XAUUSD
ASSUMED_LOT = 0.12  # current sizing on these accounts; only scales the $ column


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="demo1_m3")
    p.add_argument("--lot", type=float, default=ASSUMED_LOT, help="lot size for the $ estimate")
    return p.parse_args()


def read_filtered(account: str) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "entry_filtered":
            continue
        # direction: explicit field if the engine ever starts logging one,
        # otherwise the reason text always begins "BUY ..." / "SELL ...".
        direction = e.get("direction")
        if not direction:
            m = DIRECTION_RE.match(e.get("reason", ""))
            if not m:
                continue
            direction = m.group(1)
        try:
            e["_ts"] = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        e["_direction"] = direction
        out.append(e)
    return out


def main() -> None:
    args = parse_args()
    account = validate_account_name(args.account)
    config = load_config(account)
    events = read_filtered(account)
    if not events:
        print(f"{account}: no entry_filtered decisions logged yet.")
        return

    since = min(e["_ts"] for e in events) - timedelta(days=1)
    now = datetime.now(timezone.utc)
    connector = MT5Connector(config.mt5)
    connector.connect()
    try:
        mt5_utc_offset(connector, config.symbol)  # kept for parity with other scripts
        df = get_ohlc_range(connector, config.symbol, config.timeframe, since, now)
    finally:
        connector.disconnect()

    tp_usd, sl_usd = config.take_profit_usd, config.stop_loss_usd
    usd = args.lot * USD_PER_LOT_PER_DOLLAR
    print(f"{'=' * 78}\n{account}: {len(events)} blocked signals   "
          f"(target ${tp_usd:.2f}, stop ${sl_usd:.2f}, assuming {args.lot} lot)\n{'=' * 78}")

    saved, cost, undecided, ambiguous = 0.0, 0.0, 0, 0
    for e in events:
        ts = e["_ts"]
        prior = df[df.index < ts]
        if prior.empty:
            continue
        entry = float(prior.iloc[-1]["close"])
        is_buy = e["_direction"] == "BUY"
        tp = entry + tp_usd if is_buy else entry - tp_usd
        sl = entry - sl_usd if is_buy else entry + sl_usd

        outcome, pl = "UNDECIDED (still open)", 0.0
        for _, c in df[df.index >= ts].iterrows():
            hi, lo = float(c["high"]), float(c["low"])
            hit_tp = hi >= tp if is_buy else lo <= tp
            hit_sl = lo <= sl if is_buy else hi >= sl
            if hit_tp and hit_sl:
                outcome, pl = "AMBIGUOUS (both in one candle)", 0.0
                ambiguous += 1
                break
            if hit_tp:
                outcome, pl = "would have WON", tp_usd * usd
                cost += pl          # a win we missed = the filter cost us this
                break
            if hit_sl:
                outcome, pl = "would have LOST", -sl_usd * usd
                saved += sl_usd * usd   # a loss we avoided = the filter saved us this
                break
        else:
            undecided += 1

        why = e.get("reason", "").split("SKIPPED by entry filter: ")[-1].split(" (cross")[0]
        print(f"  [{ts.strftime('%m-%d %H:%M')} UTC] {e['_direction']:<4} entry ~{entry:.2f}  "
              f"-> {outcome}{'' if pl == 0 else f'  (${pl:+.2f})'}")
        print(f"       blocked because: {why}")

    decided = len(events) - undecided - ambiguous
    print(f"\n  {decided} decided: filter avoided ${saved:.2f} of losses, "
          f"missed ${cost:.2f} of wins  ->  net ${saved - cost:+.2f}")
    if undecided:
        print(f"  {undecided} still undecided (not enough candles yet)")
    if ambiguous:
        print(f"  {ambiguous} ambiguous (target and stop in the same candle)")
    print(f"\n  NOTE: single-trade counterfactuals. Had these been taken, later signals\n"
          f"  would have played out differently -- this answers 'was each skip right?',\n"
          f"  not 'what would the account total be?'.")


if __name__ == "__main__":
    main()
