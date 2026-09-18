"""Is trading WITHOUT a stop loss actually better? Tested on real trades.

User, 2026-09-18, after three days live:
  *"normally my strategy, I think we don't have stop loss in my strategy, it
  is good because most time, like our demo, the stop loss area comes to the
  price and reverses. EMA13/21 cross, grab profit -- I think this is best.
  And the 15-min, it is also working. We need to deeply analyze this with
  the data and candle behaviour and market condition. Think like a
  professional trader."*

That is a testable claim, so this tests it rather than agreeing with it.

THE CORE QUESTION: for every real trade, replay the candles it lived
through and measure how far price went AGAINST it (MAE) in PRICE -- $/oz,
lot-size independent, directly comparable to a stop distance. Then ask, for
a range of stop distances: had that stop been in place, which trades would
it have closed, and what would that have done to the money?

  a stop CUTS a trade that went on to lose MORE          -> the stop SAVED money
  a stop CUTS a trade that went on to WIN                -> the stop COST money
  a stop CUTS a trade that went on to lose LESS           -> the stop COST money

"The stop area comes to the price and reverses" is precisely the middle
line. If it happens often, no-stop wins. If losers usually keep going, the
stop wins. The data says which.

ALSO: the M15 trend rule (does trading WITH it beat trading against it),
every exit category, and a per-day view of market conditions.

WHICH CANDLES COUNT. The bot enters ~2 seconds after a candle OPENS, so
the candle it entered on is included (analyze_trade_path.py drops it and
misses the first 3-5 minutes of every trade). Never earlier than that: a
pre-entry buffer once produced winners deeper than their own stop, which is
impossible. The candle a trade EXITED in is only partly the trade's -- a
swap exit happens 2 seconds into it, a TP somewhere inside it -- so it is
kept out of the firm measurement. A stop level that ONLY that exit candle
reached cannot be placed before or after the exit from candles, so those
trades are counted as AMBIGUOUS and kept out of the verdict, not guessed.
The exit price itself is always a firm point on the path.

    python scripts/analyze_stop_question.py --accounts demo2_m3,demo2_m5,live2_m3,live2_m5 --since "2026-09-14 00:00:00"

Read-only. Places nothing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

COLOMBO = ZoneInfo("Asia/Colombo")
OZ_PER_LOT = 100.0
STOPS = [5.0, 7.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5,live2_m3,live2_m5")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def read_decisions(account: str) -> tuple[dict, list]:
    """exit category by ticket, and every entry record (no ticket on those)."""
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    exits: dict = {}
    entries: list = []
    if not path.is_file():
        return exits, entries
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        act = e.get("action")
        if act == "trade_exited" and e.get("ticket") is not None:
            exits[e["ticket"]] = e.get("category", "exited")
        elif act == "trade_closed_tp" and e.get("ticket") is not None:
            exits.setdefault(e["ticket"], "broker_close (TP or by hand)")
        elif act == "trade_entered":
            try:
                ts = datetime.fromisoformat(e["timestamp"])
            except (KeyError, ValueError):
                continue
            entries.append({"ts": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                            "direction": e.get("direction"),
                            "aligned": e.get("htf_aligned"),
                            "gap": e.get("gap")})
    return exits, entries


def match_entry(entries: list, direction: str, when: datetime) -> dict | None:
    """The logged entry nearest this trade's open, same direction, within 90s.
    The entry record carries no ticket, so time + direction is the join."""
    best, best_d = None, 90.0
    for e in entries:
        if e["direction"] != direction:
            continue
        d = abs((e["ts"] - when).total_seconds())
        if d <= best_d:
            best, best_d = e, d
    return best


def line(label: str, rows: list, width: int = 22) -> None:
    if not rows:
        print(f"    {label:<{width}} none")
        return
    wins = [r for r in rows if r["profit"] > 0]
    pl = sum(r["profit"] for r in rows)
    print(f"    {label:<{width}} {len(rows):>4} trades  {len(wins):>3}W {len(rows) - len(wins):>3}L "
          f"  {100 * len(wins) / len(rows):>4.0f}%   {money(pl):>11}   "
          f"{money(pl / len(rows)):>9}/trade")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 96)
    print("IS NO-STOP BETTER?  real trades, candles replayed, MAE in PRICE ($/oz)")
    print(f"since {since:%Y-%m-%d %H:%M} UTC")
    print("=" * 96)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = mt5_utc_offset(connector, config.symbol)
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(days=1), now)
        finally:
            connector.disconnect()

        exits, entries = read_decisions(account)
        rows, skipped = [], 0
        bar = timedelta(minutes=minutes_for(config.timeframe))
        for t in raw:
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            exit_utc = t["exit_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            # firm: from the candle the trade entered on, up to (not incl.)
            # the candle it exited in.  full: that exit candle as well.
            firm = df[(df.index > entry_utc - bar) & (df.index + bar <= exit_utc)]
            full = df[(df.index > entry_utc - bar) & (df.index <= exit_utc)]
            if full.empty:
                skipped += 1
                continue
            entry = float(t["entry_price"])
            exit_px = float(t["exit_price"])
            sign = 1.0 if t["direction"] == "BUY" else -1.0

            def excursions(w):
                if w.empty:
                    return 0.0, 0.0
                if sign > 0:
                    return entry - float(w["low"].min()), float(w["high"].max()) - entry
                return float(w["high"].max()) - entry, entry - float(w["low"].min())

            at_exit = sign * (exit_px - entry)        # where it actually closed
            mae_firm, mfe_firm = excursions(firm)
            mae_firm = max(mae_firm, -at_exit, 0.0)
            mae_full, mfe_full = excursions(full)
            mae_full = max(mae_full, mae_firm)
            mae, mfe = mae_firm, max(mfe_firm, at_exit, 0.0)
            logged = match_entry(entries, t["direction"], entry_utc)
            rows.append({
                "profit": float(t["profit"]),
                "volume": float(t["volume"]),
                "mae": mae,
                "mae_full": mae_full,
                "mfe": mfe,
                "at_exit": at_exit,
                "exit": exits.get(t["ticket"], "unknown"),
                "aligned": logged["aligned"] if logged else None,
                "day": t["entry_time"].astimezone(COLOMBO).date(),
                "entry_utc": entry_utc,
            })

        print(f"\n{'=' * 96}")
        stop_txt = f"${config.stop_loss_usd:.2f}" if config.stop_loss_usd else "NONE"
        bs = getattr(config, "broker_backstop_usd", None)
        print(f"{account}   {config.timeframe}   stop {stop_txt}   "
              f"backstop {'$%.2f' % bs if bs else 'none'}   "
              f"target ${config.take_profit_usd:.2f}")
        print("=" * 96)
        if not rows:
            print("  no trades in this window")
            continue

        # ---- 1. overall -------------------------------------------------
        wins = [r for r in rows if r["profit"] > 0]
        losses = [r for r in rows if r["profit"] <= 0]
        gross_w = sum(r["profit"] for r in wins)
        gross_l = sum(r["profit"] for r in losses)
        print("\n  1. OVERALL")
        print(f"    {len(rows)} trades   {len(wins)}W / {len(losses)}L   "
              f"win rate {100 * len(wins) / len(rows):.0f}%   net {money(gross_w + gross_l)}")
        if wins:
            print(f"    average win   {money(gross_w / len(wins))}")
        if losses:
            print(f"    average loss  {money(gross_l / len(losses))}")
        if wins and losses:
            pf = gross_w / abs(gross_l) if gross_l else float("inf")
            print(f"    profit factor {pf:.2f}   (gross win / gross loss; above 1.0 = profitable)")

        # ---- 2. how deep do trades go against us? -----------------------
        mae_all = [r["mae"] for r in rows]
        mae_w = [r["mae"] for r in wins]
        mae_l = [r["mae"] for r in losses]
        print("\n  2. HOW FAR PRICE WENT AGAINST THE TRADE (MAE, $/oz)")
        print(f"    all trades   median ${statistics.median(mae_all):.2f}   "
              f"worst ${max(mae_all):.2f}")
        if mae_w:
            print(f"    winners      median ${statistics.median(mae_w):.2f}   "
                  f"worst ${max(mae_w):.2f}   <- pain a winner took before winning")
        if mae_l:
            print(f"    losers       median ${statistics.median(mae_l):.2f}   "
                  f"worst ${max(mae_l):.2f}")
        if bs:
            closest = max(mae_all)
            print(f"    backstop ${bs:.2f} -- deepest any trade went: ${closest:.2f} "
                  f"({100 * closest / bs:.0f}% of the way there)")

        # ---- 3. THE STOP SIMULATION ------------------------------------
        print("\n  3. HAD A STOP BEEN IN PLACE -- what would it have done?")
        print(f"    {'stop':>6} {'hit':>5} {'won':>5} {'lost':>5} {'lost':>5}"
              f" {'ambig':>6} {'saved':>11} {'cost':>11} {'NET':>11}")
        print(f"    {'':>6} {'':>5} {'after':>5} {'more':>5} {'less':>5}")
        verdicts = []
        ordered = sorted(rows, key=lambda r: r["entry_utc"])
        first_half = ordered[: len(ordered) // 2]
        second_half = ordered[len(ordered) // 2:]
        for s in STOPS:
            clean = [r for r in rows if r["mae"] >= s]
            ambig = [r for r in rows if r["mae"] < s <= r["mae_full"]]
            hit = clean
            saved = cost = 0.0
            recovered = lost_more = lost_less = 0
            for r in clean:
                stopped_pl = -s * r["volume"] * OZ_PER_LOT
                delta = stopped_pl - r["profit"]        # what the stop CHANGES
                if r["profit"] > 0:
                    recovered += 1
                    cost += -delta if delta < 0 else 0
                elif delta > 0.005:
                    lost_more += 1
                    saved += delta
                elif delta < -0.005:
                    lost_less += 1
                    cost += -delta
            net = saved - cost
            halves = []
            for part in (first_half, second_half):
                h = 0.0
                for r in part:
                    if r["mae"] >= s:
                        h += (-s * r["volume"] * OZ_PER_LOT) - r["profit"]
                halves.append(h)
            verdicts.append((s, net, halves))
            print(f"    ${s:>5.2f} {len(hit):>5} {recovered:>5} {lost_more:>5} {lost_less:>5}"
                  f" {len(ambig):>6} {money(saved):>11} {money(-cost):>11} {money(net):>11}"
                  f"   halves {money(halves[0])} / {money(halves[1])}")
        print("    'won after' = the stop would have killed a trade that went on to WIN --")
        print("    exactly the 'stop area comes to price and reverses' case.")
        print("    'halves' = the same NET on the earlier half of trades / the later half.")
        if config.stop_loss_usd:
            print(f"    NOTE: this account already has a ${config.stop_loss_usd:.2f} stop, so no trade")
            print(f"    could go deeper than that -- rows at or beyond ${config.stop_loss_usd:.2f} "
                  f"measure nothing here.")
        # Same bar as the M3 stop fit (2026-09-13): a stop only counts if it
        # helps in BOTH halves -- one good day must not decide it.
        robust = [v for v in verdicts if v[1] > 0 and all(h > 0 for h in v[2])]
        best = max(verdicts, key=lambda v: v[1])
        if robust:
            b = max(robust, key=lambda v: v[1])
            print(f"    VERDICT: a ${b[0]:.2f} stop would have helped, by {money(b[1])}, in BOTH halves.")
            print("             The no-stop thesis does NOT hold on this account's data.")
        elif best[1] > 0:
            print(f"    VERDICT: a ${best[0]:.2f} stop shows {money(best[1])} overall but NOT in both")
            print("             halves -- one stretch carries it. Not evidence against no-stop.")
        else:
            print(f"    VERDICT: NO stop distance tested would have helped (best ${best[0]:.2f}: "
                  f"{money(best[1])}).")
            print("             Every stop killed more recoveries than it saved on losers.")

        # ---- 3b. the recovery itself -----------------------------------
        losers_deep = [r for r in losses if r["mae"] > 0]
        if losers_deep:
            back = [r["mae"] + r["at_exit"] for r in losers_deep]   # worst point -> exit
            print("\n  3b. LOSERS -- how far did price come BACK before the swap closed them?")
            print(f"    median: worst point ${statistics.median([r['mae'] for r in losers_deep]):.2f}"
                  f" against, closed at ${statistics.median([-r['at_exit'] for r in losers_deep]):.2f}"
                  f" against -> recovered ${statistics.median(back):.2f} before exit")
            print(f"    {sum(1 for b in back if b >= 2.0)}/{len(back)} losers recovered $2+ "
                  f"from their worst point before the swap closed them.")
            print("    This is the 'price comes to the stop area and reverses' effect, measured.")

        # ---- 4. every exit category ------------------------------------
        print("\n  4. EVERY EXIT CATEGORY")
        by_exit: dict = defaultdict(list)
        for r in rows:
            by_exit[r["exit"]].append(r)
        for cat, rs in sorted(by_exit.items(), key=lambda kv: -len(kv[1])):
            line(cat, rs, 30)

        # ---- 5. the M15 trend rule -------------------------------------
        print("\n  5. THE M15 TREND RULE -- with the trend vs against it")
        line("WITH the M15 trend", [r for r in rows if r["aligned"] is True])
        line("AGAINST it", [r for r in rows if r["aligned"] is False])
        unmatched = [r for r in rows if r["aligned"] is None]
        if unmatched:
            print(f"    ({len(unmatched)} trade(s) could not be matched to their entry record)")

        # ---- 6. market condition, day by day ---------------------------
        print("\n  6. DAY BY DAY -- market condition")
        daily = df.copy()
        daily["day"] = [ts.astimezone(COLOMBO).date() for ts in daily.index]
        for day in sorted({r["day"] for r in rows}):
            rs = [r for r in rows if r["day"] == day]
            d = daily[daily["day"] == day]
            if d.empty:
                line(str(day), rs, 12)
                continue
            rng = float(d["high"].max() - d["low"].min())
            move = float(d["close"].iloc[-1] - d["open"].iloc[0])
            eff = abs(move) / rng if rng else 0.0
            shape = "TRENDING" if eff >= 0.5 else ("mixed" if eff >= 0.25 else "CHOPPY")
            wins_d = [r for r in rs if r["profit"] > 0]
            print(f"    {day}  range ${rng:>6.2f}  net {move:>+7.2f}  {shape:<9} "
                  f"{len(rs):>3} trades {len(wins_d):>3}W  {money(sum(r['profit'] for r in rs)):>10}")

        if skipped:
            print(f"\n  ({skipped} trade(s) skipped: no candles covered their window)")

    print(f"\n{'=' * 96}")
    print("MAE is read from candle highs/lows: how FAR price went, not in what ORDER inside")
    print("one candle. A stop level reached ONLY in the candle a trade exited in cannot be")
    print("placed before or after the exit -- those trades are AMBIGUOUS, kept out of the")
    print("saved/cost figures. If AMBIGUOUS is large next to 'hit', read that row loosely.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED — the broker clock offset cannot be measured.")
        print(f"  {exc}")
        raise SystemExit(1)
