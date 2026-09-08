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

    python scripts/deploy_report.py --since "2026-09-08 11:39:00"
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
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
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
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    accounts = [validate_account_name(a) for a in args.accounts.split(",")]

    print("=" * 88)
    print(f"DEPLOY REPORT — everything since {args.since} UTC "
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
        print(f"{account} ({c.timeframe})   TP ${c.take_profit_usd:.2f}  stop ${c.stop_loss_usd:.2f}  "
              f"breakeven {c.breakeven_trigger_usd}  swap_immediate={c.swap_immediate}")
        print(f"   TP-runner: {runner}")
        print("=" * 88)

        if not trades:
            print("  No closed trades in this window.")
            per_account[account] = {"n": 0, "pl": 0.0}
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
        per_account[account] = {"n": len(trades), "pl": total}
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
    print(f"\n{'=' * 88}")
    print("demo1 (new rules) vs demo2 (control) — PER TRADE, since lot sizes can differ")
    print("=" * 88)
    for leg in ("m1", "m3"):
        a, b = f"demo1_{leg}", f"demo2_{leg}"
        if a in per_account and b in per_account and per_account[a]["n"] and per_account[b]["n"]:
            pa, pb = per_account[a], per_account[b]
            diff = pa["pl"] / pa["n"] - pb["pl"] / pb["n"]
            print(f"  {leg.upper()}:  demo1 ${pa['pl'] / pa['n']:+.2f}/trade ({pa['n']} trades)   "
                  f"demo2 ${pb['pl'] / pb['n']:+.2f}/trade ({pb['n']} trades)   "
                  f"demo1 {'ahead' if diff > 0 else 'behind'} by ${abs(diff):.2f}/trade")
    print("\nA day or two is noise. What matters over weeks is the per-trade gap and, for the")
    print("runner, how many locked trades actually RAN.")


if __name__ == "__main__":
    main()
