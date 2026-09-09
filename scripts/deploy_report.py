"""Full report on everything traded since a deployment.

Built 2026-09-09 for the question "what has happened since we deployed,
with every detail and the reason for each trade". Answers it in one
place instead of four scripts:

  1. Every trade, with the engine's OWN reason for entering and exiting.
  2. Exit-reason totals -- where the money actually went.
  3. The TP-runner scorecard: how often the broker take-profit was
     successfully removed, how often a trade then locked, and how often
     it went on to RUN. A failed removal is not a loss -- the trade just
     closes at target as before -- but it does mean the rule never got
     to act, so the two are counted separately.
  4. Swap frequency, as a whipsaw check. Removing the 2-candle debounce
     on 2026-09-08 was expected to increase rapid flips; a swap that
     both opens and closes within a few candles is the signature.
  5. demo1 against demo2 over the same window, per trade rather than in
     total, since the two can hold different lot sizes.

    python scripts/deploy_report.py --today
    python scripts/deploy_report.py --since "2026-09-08 11:39:00"
    python scripts/deploy_report.py --today
    python scripts/deploy_report.py --since "2026-09-08 11:39:00" --accounts demo1_m1,demo1_m3,demo2_m1,demo2_m3

Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from bot.analytics import get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.mt5_connector import MT5Connector

COLOMBO = ZoneInfo("Asia/Colombo")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo1_m1,demo1_m3,demo2_m1,demo2_m3")
    p.add_argument("--since", help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--today", action="store_true",
                   help="from the start of the CURRENT trading session (04:00 Colombo). "
                        "Saves converting Colombo to UTC by hand every morning.")
    p.add_argument("--offset-hours", type=float, default=None)
    return p.parse_args()


def read_events(account: str, since: datetime) -> list[dict]:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_ts"] = datetime.fromisoformat(e["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if e["_ts"] >= since:
                out.append(e)
    out.sort(key=lambda e: e["_ts"])
    return out


def short_reason(reason: str) -> str:
    """The engine's reason, trimmed to the part that says WHY."""
    r = (reason or "").strip()
    for marker in (" (ema13=", ", gap=", " (this candle:", " (armed at"):
        if marker in r:
            r = r.split(marker)[0]
    return r[:78]


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)
    if args.today:
        # The session opens 04:00 Colombo. Before that hour the current
        # session began YESTERDAY, so step back a day rather than
        # reporting an empty window.
        local = now.astimezone(COLOMBO)
        start = local.replace(hour=4, minute=0, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        since = start.astimezone(timezone.utc)
    elif args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        raise SystemExit("Give --today, or --since \"YYYY-MM-DD HH:MM:SS\" in UTC.")
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    print("=" * 88)
    # Print the resolved timestamp, not the raw argument: with --today
    # there is no --since and the header read "since None UTC".
    print(f"DEPLOY REPORT — everything since {since:%Y-%m-%d %H:%M:%S} UTC "
          f"({since.astimezone(COLOMBO):%d %b %H:%M} Colombo)")
    print("=" * 88)

    per_account = {}

    for account in accounts:
        c = load_config(account)
        events = read_events(account, since)

        connector = MT5Connector(c.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, c.symbol))
            trades = get_closed_trades_range(c.symbol, c.execution.magic_number, since, now, offset)
        finally:
            connector.disconnect()
        trades.sort(key=lambda t: t["entry_time"])

        runner = "OFF" if c.tp_runner_trail_usd is None else (
            f"lock +${c.take_profit_usd - c.tp_runner_lock_below_usd:.2f}, "
            f"trail ${c.tp_runner_trail_usd:.2f}, arm ${c.tp_runner_arm_before_usd:.2f} early")
        print(f"\n{'=' * 88}")
        # A null stop is a real configuration now (demo2_m3), so nothing
        # here may assume a number. Same fault as bot/config.py's second
        # guard: the reporting tools were never told the stop is optional.
        stop_txt = "NO STOP" if c.stop_loss_usd is None else f"${c.stop_loss_usd:.2f}"
        tp_txt = f"${c.take_profit_usd:.2f}"
        if getattr(c, "htf_trend_take_profit_usd", None) is not None:
            tp_txt = (f"${c.htf_trend_take_profit_usd:.2f} with the {c.htf_trend_timeframe} "
                      f"trend / ${c.take_profit_usd:.2f} against")
        print(f"{account} ({c.timeframe})   TP {tp_txt}  stop {stop_txt}  "
              f"breakeven {c.breakeven_trigger_usd}  swap_immediate={c.swap_immediate}")
        print(f"   TP-runner: {runner}")
        print("=" * 88)

        if not trades:
            print("  No closed trades in this window.")
            per_account[account] = {"n": 0, "pl": 0.0, "trades": [], "cfg": c}
            continue

        # ---- every trade, with the engine's own words -----------------
        exits = {e.get("ticket"): e for e in events
                 if e.get("action") in ("trade_exited", "trade_closed_tp") and e.get("ticket")}
        by_reason: dict[str, list[float]] = defaultdict(list)

        print(f"  {'time':<10}{'dir':<6}{'lots':>6}{'entry':>10}{'exit':>10}{'move$':>8}{'P/L':>10}  why it closed")
        for t in trades:
            move = ((float(t["exit_price"]) - float(t["entry_price"])) if t["direction"] == "BUY"
                    else (float(t["entry_price"]) - float(t["exit_price"])))
            ev = exits.get(int(t["ticket"]))
            cat = (ev or {}).get("category") or ("take_profit" if ev and ev.get("action") == "trade_closed_tp" else "?")
            by_reason[cat].append(float(t["profit"]))
            print(f"  {t['entry_time'].astimezone(COLOMBO):%H:%M:%S}{t['direction']:>6}"
                  f"{t['volume']:>6}{t['entry_price']:>10.2f}{t['exit_price']:>10.2f}"
                  f"{move:>+8.2f}{t['profit']:>+10.2f}  {short_reason((ev or {}).get('reason', ''))}")

        wins = sum(1 for t in trades if t["profit"] > 0)
        total = sum(t["profit"] for t in trades)
        per_account[account] = {"n": len(trades), "pl": total, "trades": trades, "cfg": c}
        print(f"\n  {len(trades)} trades, {wins} wins ({100 * wins / len(trades):.0f}%), "
              f"net ${total:+.2f}, ${total / len(trades):+.2f}/trade")

        # ---- where the money went ------------------------------------
        print(f"\n  WHERE THE MONEY WENT")
        for cat, pls in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
            print(f"    {cat:<28} n={len(pls):<4} ${sum(pls):>+9.2f}   ${sum(pls) / len(pls):>+7.2f}/trade")

        # ---- the runner ----------------------------------------------
        armed = [e for e in events if e.get("action") == "tp_runner_armed"]
        removed = [e for e in armed if "REMOVAL FAILED" not in (e.get("reason") or "")]
        locked = [e for e in events if e.get("action") == "tp_runner_locked"]
        trailed = [e for e in events if e.get("action") == "tp_runner_trailed"]
        if c.tp_runner_trail_usd is not None:
            print(f"\n  TP-RUNNER")
            print(f"    reached the arm point : {len(armed)}")
            print(f"    take-profit removed   : {len(removed)}"
                  f"{f'  ({len(armed) - len(removed)} FAILED — trade closed at target as before)' if len(removed) < len(armed) else ''}")
            print(f"    locked past target    : {len(locked)}")
            print(f"    then RAN further      : {len(set(e.get('ticket') for e in trailed))}"
                  f"   <- the number that decides whether this rule pays")

        # ---- whipsaw --------------------------------------------------
        swaps = [e for e in events if (e.get("category") == "swapped_confirmed_reversal"
                                       or e.get("category") == "swapped_reversal")]
        if swaps:
            swap_pl = [t["profit"] for t in trades
                       if int(t["ticket"]) in {e.get("ticket") for e in swaps}]
            print(f"\n  SWAPS (whipsaw check — the debounce was removed 2026-09-08)")
            print(f"    swap exits: {len(swaps)} of {len(trades)} trades "
                  f"({100 * len(swaps) / len(trades):.0f}%)"
                  + (f", ${sum(swap_pl):+.2f} total, ${sum(swap_pl) / len(swap_pl):+.2f}/trade"
                     if swap_pl else ""))

    # ---- demo1 vs demo2 ---------------------------------------------
    # Only trades BOTH accounts actually took can compare the rules. demo1_m1
    # carries a session window demo2_m1 does not, so a straight per-trade
    # average silently compares "demo1's rules" against "demo1's rules plus
    # three extra hours of market" -- and credits the difference to the rules.
    # Pair on entry time + direction; report the unpaired ones separately, as
    # a session-window result, which is what they are.
    print(f"\n{'=' * 88}")
    print("demo1 (new rules) vs demo2 (control)")
    print("=" * 88)
    for leg in ("m1", "m3"):
        a, b = f"demo1_{leg}", f"demo2_{leg}"
        if not (per_account.get(a, {}).get("n") and per_account.get(b, {}).get("n")):
            continue
        ta, tb = per_account[a]["trades"], list(per_account[b]["trades"])
        pairs, solo_a = [], []
        for x in ta:
            match = next((y for y in tb if y["direction"] == x["direction"]
                          and abs((y["entry_time"] - x["entry_time"]).total_seconds()) <= 90), None)
            if match:
                tb.remove(match)
                pairs.append((x, match))
            else:
                solo_a.append(x)

        print(f"\n  {leg.upper()} — {len(pairs)} trades BOTH accounts took (the only fair comparison)")
        if pairs:
            print(f"    {'time':<10}{'dir':<6}{'demo1':>10}{'demo2':>10}{'diff':>10}  what made the difference")
            ca, cb = per_account[a]["cfg"], per_account[b]["cfg"]
            for x, y in pairs:
                d = float(x["profit"]) - float(y["profit"])
                # Attribute the difference to the rule that actually caused it.
                # Labelling purely on the size of the gap credited the runner
                # for a LOSING trade on 2026-09-09, where the real cause was
                # demo1's tighter stop ($7 vs $10) -- and the runner cannot
                # act on a loser at all, since it only arms in profit.
                def moved(t):
                    return ((float(t["exit_price"]) - float(t["entry_price"])) if t["direction"] == "BUY"
                            else (float(t["entry_price"]) - float(t["exit_price"])))
                move, move_b = moved(x), moved(y)
                # "Stops differ" only explains a loss if a stop was actually
                # REACHED. On 2026-09-09 at 09:36 both accounts swapped out on
                # the opposite cross at -$6, nowhere near either stop, and the
                # $2.16 gap was entry price -- but the label still blamed the
                # stop sizes.
                hit_a = ca.stop_loss_usd is not None and move <= -(ca.stop_loss_usd - 0.50)
                hit_b = cb.stop_loss_usd is not None and move_b <= -(cb.stop_loss_usd - 0.50)
                if float(x["profit"]) <= 0 and float(y["profit"]) <= 0:
                    if hit_a or hit_b:
                        note = (f"both lost — demo1 stop "
                                f"{'none' if ca.stop_loss_usd is None else f'${ca.stop_loss_usd:.0f}'}, "
                                f"demo2 stop "
                                f"{'none' if cb.stop_loss_usd is None else f'${cb.stop_loss_usd:.0f}'}")
                    else:
                        note = ("both lost on the opposite cross, neither reached its stop "
                                "— entry/exit price only")
                elif float(x["profit"]) > 0 and float(y["profit"]) > 0 and abs(d) > 2:
                    if move > ca.take_profit_usd + 0.01:
                        note = f"RUNNER RAN — held to +${move:.2f} past its ${ca.take_profit_usd:.0f} target"
                    elif d < 0:
                        note = f"runner locked early at +${move:.2f}, control rode to its target"
                    else:
                        note = "different exit rule"
                elif (float(x["profit"]) > 0) != (float(y["profit"]) > 0):
                    note = "one won, one lost — different exit rules, not the runner"
                else:
                    note = "entry/exit price only"
                print(f"    {x['entry_time'].astimezone(COLOMBO):%H:%M:%S}{x['direction']:>6}"
                      f"{x['profit']:>+10.2f}{y['profit']:>+10.2f}{d:>+10.2f}  {note}")
            pa = sum(float(x['profit']) for x, _ in pairs)
            pb = sum(float(y['profit']) for _, y in pairs)
            print(f"    {'TOTAL':<16}{pa:>+10.2f}{pb:>+10.2f}{pa - pb:>+10.2f}"
                  f"   = ${(pa - pb) / len(pairs):+.2f}/trade from the RULES")
            runner_d = sum(float(x["profit"]) - float(y["profit"]) for x, y in pairs
                           if float(x["profit"]) > 0 and float(y["profit"]) > 0)
            other_d = (pa - pb) - runner_d
            print(f"    of which: ${runner_d:+.2f} on trades BOTH won (where the runner can act)")
            print(f"              ${other_d:+.2f} on losers and split results "
                  f"(stop size, exit rules, entry price)")
        if solo_a:
            s_pl = sum(float(x["profit"]) for x in solo_a)
            print(f"\n    {len(solo_a)} trades only {a} took (session-window difference, NOT the rules):"
                  f" ${s_pl:+.2f}, ${s_pl / len(solo_a):+.2f}/trade")
        if tb:
            s_pl = sum(float(y["profit"]) for y in tb)
            print(f"    {len(tb)} trades only {b} took: ${s_pl:+.2f}")

    print("\nA day is noise. The number to watch over weeks is the paired per-trade gap,")
    print("and for the runner, how many locked trades actually RAN.")


if __name__ == "__main__":
    main()
