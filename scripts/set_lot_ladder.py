"""Set an account's balance-to-lots ladder, and show what it risks.

User's ladder for the M5 legs, 2026-09-09:
    up to $200   0.01
    $200-$500    0.02
    $500-$1,000  0.04
    above $1,000 0.08

bot/risk/position_sizing.py picks the FIRST tier whose max_balance >= the
balance, so a tier's max_balance is inclusive and the last must be
unbounded.

Lot size is the one setting that silently changes how much every other
rule costs, so this prints RISK AS A PERCENTAGE OF BALANCE at the point
each tier is worst -- the moment the balance crosses into it, when the
bigger lots apply to the smallest balance that qualifies. A ladder can
look tidy and still risk 20% of the account at one step.

    python scripts/set_lot_ladder.py --accounts demo1_m5,demo2_m5 \
        --tiers "200:0.01,500:0.02,1000:0.04,none:0.08"
    ... --apply

Refuses a ladder that is not ascending in both balance and lots.
"""
from __future__ import annotations

import argparse
import sys

import yaml

sys.path.insert(0, ".")

from bot.config import PROJECT_ROOT, validate_account_name


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", required=True)
    p.add_argument("--tiers", required=True,
                   help='"max_balance:lots,..." ascending, last max_balance "none"')
    p.add_argument("--apply", action="store_true")
    return p.parse_args()


def parse_tiers(raw: str) -> list[dict]:
    tiers = []
    for part in raw.split(","):
        cap, lots = part.split(":")
        cap = cap.strip().lower()
        tiers.append({"max_balance": None if cap in ("none", "null") else float(cap),
                      "lots": float(lots)})
    return tiers


def main() -> None:
    args = parse_args()
    tiers = parse_tiers(args.tiers)

    if tiers[-1]["max_balance"] is not None:
        sys.exit("REFUSING: the last tier must have max_balance 'none', or a balance above "
                 "the highest tier would fall through with no lot size.")
    caps = [t["max_balance"] for t in tiers[:-1]]
    if caps != sorted(caps):
        sys.exit("REFUSING: tiers must ascend by max_balance — position_sizing takes the "
                 "FIRST tier that fits, so an out-of-order ladder silently uses the wrong row.")
    lots = [t["lots"] for t in tiers]
    if lots != sorted(lots):
        sys.exit("REFUSING: lots must ascend with balance.")
    if any(t["lots"] < 0.01 for t in tiers):
        sys.exit("REFUSING: 0.01 is the broker minimum lot.")

    for account in [validate_account_name(a) for a in args.accounts.split(",")]:
        path = PROJECT_ROOT / "config" / f"settings.{account}.yaml"
        if not path.exists():
            print(f"  {account}: no config, skipped.")
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        stop = doc.get("stop_loss_usd")

        print("=" * 78)
        print(f"{account}   stop {'none' if stop is None else f'${float(stop):.2f}'}")
        print("=" * 78)
        old = doc.get("position_sizing") or []

        def rung(t: dict) -> str:
            """One tier as text. The unbounded last tier has max_balance
            None, and a conditional expression that still evaluates
            format() on that branch raises -- which is exactly how the
            first version of this line died."""
            cap = t["max_balance"]
            return f"{'any' if cap is None else '<=' + format(cap, 'g')}={t['lots']}"

        print("  was: " + (", ".join(rung(t) for t in old) if old else "(none)"))

        print(f"\n  {'balance':<20}{'lots':>7}{'risk/trade':>13}{'% of balance':>15}")
        floor = 0.0
        for t in tiers:
            cap = t["max_balance"]
            band = (f"up to ${cap:,.0f}" if floor == 0 and cap is not None else
                    f"${floor:,.0f} to ${cap:,.0f}" if cap is not None else
                    f"above ${floor:,.0f}")
            if stop is None:
                risk_txt, pct_txt = "unbounded", "— no stop"
            else:
                risk = t["lots"] * float(stop) * 100
                risk_txt = f"${risk:,.0f}"
                # Worst point in the tier: the smallest balance that
                # qualifies for these larger lots.
                worst = floor if floor > 0 else (cap or 0)
                pct_txt = f"{100 * risk / worst:.1f}%" if worst else "—"
            print(f"  {band:<20}{t['lots']:>7}{risk_txt:>13}{pct_txt:>15}")
            floor = cap if cap is not None else floor

        if stop is not None:
            worst = max((t["lots"] * float(stop) * 100) / caps[i - 1]
                        for i, t in enumerate(tiers) if i > 0 and caps[i - 1])
            print(f"\n  Worst case: {100 * worst:.1f}% of balance on a single losing trade,")
            print(f"  which happens just after the balance crosses into a bigger tier.")

        if not args.apply:
            print("\n  DRY RUN — nothing written.\n")
            continue
        doc["position_sizing"] = tiers
        path.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False),
                        encoding="utf-8")
        yaml.safe_load(path.read_text(encoding="utf-8"))
        print(f"\n  written and parsed OK: {path.name}")
        print(f"  Restart to load it:  python scripts/restart_bot.py --accounts {account} "
              f"--wait-for-flat 120 --start\n")


if __name__ == "__main__":
    main()
