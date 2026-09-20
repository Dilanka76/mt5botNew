"""Do losing entries look different from winning ones AT THE MOMENT OF ENTRY?

User, 2026-09-20: "the entry confirmation, entry and buy loss amount
reduce, this is the most important thing."

The only door still open. Smaller targets were tested and cost money; a
stop loss was tested and cost money. Both touch every trade. Entry
selection touches only the trades we skip -- the winners keep their full
size. So this looks for a fingerprint.

WHAT IT MEASURES, and the discipline in it:

* Features are read from the SIGNAL candle -- the last candle that closed
  before the trade opened. That is exactly what the bot knew when it
  decided. Nothing from the entry candle or later is used, so no result
  here can be an accident of hindsight.
* Outcome is measured in $/oz, not dollars. A 0.06-lot trade and a
  0.02-lot trade are then directly comparable and lot size cannot tilt
  any bucket.
* Every feature is split into four equal groups, and each group reports
  its win rate, its money, and its share of MONSTERS (losses worse than
  $8/oz) -- the few trades that carry a third of all damage.
* Each group's result is shown for the FIRST half and the SECOND half of
  the trades separately. A feature that only works in one half is noise,
  and the print says so rather than leaving it to be misread.

THE TRAP THIS IS BUILT AGAINST: with a dozen features, four groups and
several accounts, pure chance produces impressive-looking splits. Twelve
entry filters have already died here, and one reached live and cost $192.
So NOTHING found here is a rule. A candidate must repeat on M3 AND M5,
in BOTH halves, and then be shadow-logged on live before it is believed.

    python scripts/entry_fingerprint.py --since "2026-08-25 00:00:00" --offset-hours 3
    python scripts/entry_fingerprint.py --since "2026-09-09 00:00:00" --accounts demo2_m3,demo2_m5

Read-only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

import pandas as pd

from bot.analytics import StaleTickError, get_closed_trades_range, mt5_utc_offset
from bot.config import PROJECT_ROOT, load_config, validate_account_name
from bot.data.market_data import get_ohlc_range
from bot.indicators.adx import compute_adx
from bot.indicators.ema import compute_emas
from bot.mt5_connector import MT5Connector
from bot.timeframes import minutes_for

COLOMBO = ZoneInfo("Asia/Colombo")
OZ_PER_LOT = 100.0
MONSTER_USD_PER_OZ = 8.0     # a loss this deep is in the worst ~10%

FEATURES = [
    ("gap from EMA13", "gap", "how far price closed from the EMA13 it just crossed"),
    ("EMA13-21 spread", "ema_sep", "how far apart the two crossing lines are"),
    ("EMA21 slope (our way)", "slope", "is the slower line already moving our way"),
    ("signal candle body", "body", "share of the candle that is body, not wick"),
    ("candle pushed our way", "push", "how far the signal candle closed in our direction"),
    ("ATR14", "atr", "how big candles are right now"),
    ("ATR vs its own average", "atr_ratio", "quiet market or fast one"),
    ("place in 20-candle range", "range_pos", "1.0 = entering at the top of the recent range"),
    ("already travelled", "travel", "how far price moved our way in the last 10 candles"),
    ("ADX14", "adx", "trend strength"),
    ("spread at entry", "spread", "broker cost at that moment"),
    ("Colombo hour", "hour", "time of day"),
    # --- market STRUCTURE, never tested in this project before ----------
    ("M15 candle overlap", "m15_overlap", "1.0 = M15 candles printing side by side: a box"),
    ("M15 box width / ATR", "m15_box_atr", "small = price trapped in a tight range"),
    ("place in the M15 box", "m15_box_pos", "1.0 = at the top of the range we are buying into"),
    ("EMA crosses last 15", "crosses15", "the braiding count: how choppy the lines are"),
    ("distance from EMA21 / ATR", "displacement", "has price broken AWAY from the lines"),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="demo2_m3,demo2_m5,live2_m3,live2_m5")
    p.add_argument("--since", required=True, help='"YYYY-MM-DD HH:MM:SS", true UTC')
    p.add_argument("--offset-hours", type=float, default=None,
                   help="broker clock offset; needed when the market is closed (3)")
    p.add_argument("--groups", type=int, default=4, help="how many equal groups per feature")
    return p.parse_args()


def money(v: float) -> str:
    if abs(v) < 0.005:
        return "$0.00"
    return f"-${abs(v):,.2f}" if v < 0 else f"+${v:,.2f}"


def read_aligned(account: str) -> list:
    path = PROJECT_ROOT / "logs" / account / "decisions.jsonl"
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "trade_entered":
            continue
        try:
            ts = datetime.fromisoformat(e["timestamp"])
        except (KeyError, ValueError):
            continue
        out.append({"ts": ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                    "direction": e.get("direction"), "aligned": e.get("htf_aligned")})
    return out


def swing_points(h: pd.Series, l: pd.Series, k: int = 2):
    """Fractal swings: a high with k lower highs each side, and the same
    for lows. The textbook definition of the points HH/HL structure is
    read from -- not an indicator, the shape of price itself."""
    hi = [h.index[i] for i in range(k, len(h) - k)
          if h.iloc[i] == h.iloc[i - k:i + k + 1].max()]
    lo = [l.index[i] for i in range(k, len(l) - k)
          if l.iloc[i] == l.iloc[i - k:i + k + 1].min()]
    return hi, lo


def structure_at(htf: pd.DataFrame, hi: list, lo: list, when) -> dict:
    """Market geometry as it stood at `when`, from the last two swings of
    each kind that had already formed (a fractal needs k candles after it
    to exist, so only swings confirmed before `when` are used)."""
    ph = [t for t in hi if t <= when][-2:]
    pl = [t for t in lo if t <= when][-2:]
    out = {"structure": 0.0, "m15_box_atr": float("nan"), "m15_box_pos": float("nan")}
    if len(ph) < 2 or len(pl) < 2:
        return out
    h1, h2 = float(htf.loc[ph[0], "high"]), float(htf.loc[ph[1], "high"])
    l1, l2 = float(htf.loc[pl[0], "low"]), float(htf.loc[pl[1], "low"])
    if h2 > h1 and l2 > l1:
        out["structure"] = 1.0            # higher highs AND higher lows
    elif h2 < h1 and l2 < l1:
        out["structure"] = -1.0           # lower highs AND lower lows
    top, bottom = max(h1, h2), min(l1, l2)
    atr = float(htf.loc[:when, "atr"].iloc[-1]) if len(htf.loc[:when]) else float("nan")
    close = float(htf.loc[:when, "close"].iloc[-1])
    if atr and atr == atr:
        out["m15_box_atr"] = (top - bottom) / atr
    if top > bottom:
        out["m15_box_pos"] = (close - bottom) / (top - bottom)
    return out


def overlap_ratio(htf: pd.DataFrame, when, n: int = 5) -> float:
    """How much consecutive candles cover the same prices. Near 1.0 the
    candles print side by side -- the box the user is trying to see."""
    w = htf.loc[:when].tail(n + 1)
    if len(w) < n + 1:
        return float("nan")
    vals = []
    for i in range(1, len(w)):
        h1, l1 = float(w["high"].iloc[i - 1]), float(w["low"].iloc[i - 1])
        h2, l2 = float(w["high"].iloc[i]), float(w["low"].iloc[i])
        union = max(h1, h2) - min(l1, l2)
        if union > 0:
            vals.append(max(0.0, min(h1, h2) - max(l1, l2)) / union)
    return sum(vals) / len(vals) if vals else float("nan")


def prepare(df: pd.DataFrame, config) -> pd.DataFrame:
    """Every feature the bot could have known, on each closed candle."""
    out = compute_emas(df, config.ema_periods)
    out = compute_adx(out)
    prev_close = out["close"].shift(1)
    tr = pd.concat([out["high"] - out["low"],
                    (out["high"] - prev_close).abs(),
                    (out["low"] - prev_close).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    out["atr_ratio"] = out["atr"] / out["atr"].rolling(50).mean()
    span = out["high"].rolling(20).max() - out["low"].rolling(20).min()
    out["range_pos"] = (out["close"] - out["low"].rolling(20).min()) / span.replace(0, float("nan"))
    out["gap"] = (out["close"] - out["ema13"]).abs()
    out["ema_sep"] = (out["ema13"] - out["ema21"]).abs()
    out["body"] = (out["close"] - out["open"]).abs() / (out["high"] - out["low"]).replace(0, float("nan"))
    out["_slope_raw"] = out["ema21"] - out["ema21"].shift(5)
    out["_push_raw"] = out["close"] - out["open"]
    out["_travel_raw"] = out["close"] - out["close"].shift(10)
    side = (out["ema13"] - out["ema21"]).apply(lambda v: 1 if v > 0 else (-1 if v < 0 else 0))
    out["crosses15"] = (side != side.shift(1)).rolling(15).sum()
    out["displacement"] = (out["close"] - out["ema21"]).abs() / out["atr"].replace(0, float("nan"))
    return out


def show_feature(name: str, note: str, rows: list, key: str, groups: int) -> None:
    vals = [r[key] for r in rows if r[key] == r[key]]     # drop NaN
    usable = [r for r in rows if r[key] == r[key]]
    if len(vals) < groups * 5:
        print(f"\n  {name}: too few trades to split ({len(vals)})")
        return
    usable.sort(key=lambda r: r[key])
    size = len(usable) // groups
    half = len(rows) // 2
    order = {id(r): i for i, r in enumerate(sorted(rows, key=lambda r: r["entry_utc"]))}
    print(f"\n  {name}   ({note})")
    print(f"    {'range':<20} {'n':>4} {'win':>5} {'$/oz avg':>9} {'monsters':>9} "
          f"{'1st half':>10} {'2nd half':>10}")
    for g in range(groups):
        chunk = usable[g * size:] if g == groups - 1 else usable[g * size:(g + 1) * size]
        if not chunk:
            continue
        w = sum(1 for r in chunk if r["oz"] > 0)
        mons = sum(1 for r in chunk if r["oz"] <= -MONSTER_USD_PER_OZ)
        first = [r["oz"] for r in chunk if order[id(r)] < half]
        second = [r["oz"] for r in chunk if order[id(r)] >= half]
        lo, hi = chunk[0][key], chunk[-1][key]
        print(f"    {f'{lo:.2f} to {hi:.2f}':<20} {len(chunk):>4} {100 * w / len(chunk):>4.0f}% "
              f"{statistics.mean(r['oz'] for r in chunk):>+8.2f} "
              f"{mons:>4} ({100 * mons / len(chunk):>2.0f}%) "
              f"{statistics.mean(first) if first else float('nan'):>+9.2f} "
              f"{statistics.mean(second) if second else float('nan'):>+9.2f}")


def main() -> None:
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    print("=" * 100)
    print("ENTRY FINGERPRINT -- what the bot knew when it decided, against what happened")
    print(f"since {since:%Y-%m-%d %H:%M} UTC   (outcome in $/oz, so lot size cannot tilt anything)")
    print("=" * 100)

    for account in [validate_account_name(a.strip()) for a in args.accounts.split(",")]:
        config = load_config(account)
        connector = MT5Connector(config.mt5)
        connector.connect()
        try:
            offset = (timedelta(hours=args.offset_hours) if args.offset_hours is not None
                      else mt5_utc_offset(connector, config.symbol))
            raw = get_closed_trades_range(config.symbol, config.execution.magic_number,
                                          since, now, offset)
            df = get_ohlc_range(connector, config.symbol, config.timeframe,
                                since - timedelta(days=3), now, offset)
            # the "zoom out" chart: structure is read here, entries below
            htf_name = config.htf_trend_timeframe or "M15"
            htf = get_ohlc_range(connector, config.symbol, htf_name,
                                 since - timedelta(days=5), now, offset)
        finally:
            connector.disconnect()

        feats = prepare(df, config)
        htf = prepare(htf, config)
        swing_hi, swing_lo = swing_points(htf["high"], htf["low"])
        logged = read_aligned(account)
        bar = timedelta(minutes=minutes_for(config.timeframe))

        rows = []
        prev_exit = None
        for t in sorted(raw, key=lambda x: x["entry_time"]):
            entry_utc = t["entry_time"].astimezone(timezone.utc)
            if entry_utc < since:
                continue
            # the SIGNAL candle: the last one that had closed when the bot
            # decided. The entry candle itself was still forming.
            prior = feats[feats.index + bar <= entry_utc]
            if prior.empty:
                continue
            c = prior.iloc[-1]
            sign = 1.0 if t["direction"] == "BUY" else -1.0
            vol = float(t["volume"])
            match, best_d = None, 90.0
            for e in logged:
                if e["direction"] != t["direction"]:
                    continue
                d = abs((e["ts"] - entry_utc).total_seconds())
                if d <= best_d:
                    match, best_d = e, d
            # structure from the last HTF candle closed before entry
            htf_prior = htf.index[htf.index <= entry_utc]
            if len(htf_prior):
                st = structure_at(htf, swing_hi, swing_lo, htf_prior[-1])
                ov = overlap_ratio(htf, htf_prior[-1])
            else:
                st, ov = {"structure": 0.0, "m15_box_atr": float("nan"),
                          "m15_box_pos": float("nan")}, float("nan")
            rows.append({
                "entry_utc": entry_utc,
                "m15_overlap": ov, "m15_box_atr": st["m15_box_atr"],
                "m15_box_pos": st["m15_box_pos"] if sign > 0 else 1 - st["m15_box_pos"],
                "crosses15": float(c["crosses15"]), "displacement": float(c["displacement"]),
                "structure_agrees": st["structure"] == sign,
                "structure_against": st["structure"] == -sign,
                "oz": float(t["profit"]) / (vol * OZ_PER_LOT),
                "gap": float(c["gap"]), "ema_sep": float(c["ema_sep"]),
                "slope": sign * float(c["_slope_raw"]), "body": float(c["body"]),
                "push": sign * float(c["_push_raw"]), "atr": float(c["atr"]),
                "atr_ratio": float(c["atr_ratio"]),
                "range_pos": float(c["range_pos"]) if sign > 0 else 1 - float(c["range_pos"]),
                "travel": sign * float(c["_travel_raw"]), "adx": float(c["adx"]),
                "spread": float(c["spread"]) / 100.0,
                "hour": float(entry_utc.astimezone(COLOMBO).hour),
                "aligned": bool(match and match["aligned"]),
                "reentry": prev_exit is not None
                           and (entry_utc - prev_exit).total_seconds() <= 60,
                "dir": t["direction"],
            })
            prev_exit = t["exit_time"].astimezone(timezone.utc)

        print(f"\n{'=' * 100}")
        print(f"{account}   {config.timeframe}   {len(rows)} trades")
        print("=" * 100)
        if len(rows) < 40:
            print("  fewer than 40 trades -- any split here is noise. Shown for completeness only.")
        if not rows:
            continue

        mons = [r for r in rows if r["oz"] <= -MONSTER_USD_PER_OZ]
        print(f"  average {statistics.mean(r['oz'] for r in rows):+.2f} $/oz per trade   "
              f"{sum(1 for r in rows if r['oz'] > 0)} won   "
              f"{len(mons)} monsters (worse than -${MONSTER_USD_PER_OZ:.0f}/oz)")

        for name, key, note in FEATURES:
            show_feature(name, note, rows, key, args.groups)

        # yes/no features
        print("\n  yes/no splits")
        for label, key in (("M15 trend aligned", "aligned"), ("reversal re-entry", "reentry"),
                           ("structure agrees (HH/HL)", "structure_agrees"),
                           ("structure AGAINST us", "structure_against"),
                           ("BUY", "dir")):
            for want in ((True, False) if key != "dir" else ("BUY", "SELL")):
                sel = [r for r in rows if r[key] == want]
                if not sel:
                    continue
                w = sum(1 for r in sel if r["oz"] > 0)
                m = sum(1 for r in sel if r["oz"] <= -MONSTER_USD_PER_OZ)
                tag = label if key == "dir" and want == "BUY" else (
                    "SELL" if key == "dir" else f"{label}: {'yes' if want else 'no'}")
                print(f"    {tag:<28} {len(sel):>4} trades  {100 * w / len(sel):>4.0f}% won  "
                      f"{statistics.mean(r['oz'] for r in sel):>+6.2f} $/oz  "
                      f"{m} monster(s)")

        # what the monsters looked like
        if mons:
            print(f"\n  THE {len(mons)} MONSTERS vs everything else (median of each feature)")
            others = [r for r in rows if r not in mons]
            print(f"    {'feature':<26} {'monsters':>10} {'others':>10}")
            for name, key, _ in FEATURES:
                mv = [r[key] for r in mons if r[key] == r[key]]
                ov = [r[key] for r in others if r[key] == r[key]]
                if mv and ov:
                    print(f"    {name:<26} {statistics.median(mv):>10.2f} "
                          f"{statistics.median(ov):>10.2f}")

    print(f"\n{'=' * 100}")
    print("NOTHING HERE IS A RULE. With 12 features x 4 groups x 4 accounts, chance alone")
    print("produces striking splits. A candidate must hold on M3 AND M5, in BOTH halves,")
    print("and then be shadow-logged on live before a single dollar depends on it.")


if __name__ == "__main__":
    try:
        main()
    except StaleTickError as exc:
        print("\nMARKET CLOSED -- pass --offset-hours 3 to run this on a weekend.")
        print(f"\n  {exc}".replace("\n", "\n  "))
        raise SystemExit(1)
