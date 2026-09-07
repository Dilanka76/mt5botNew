"""Logic checks for bot/daily_loss.py.

    python3 tests/test_daily_loss.py

This is the rule that caps a bad day on a REAL-money account, so its
failure modes matter more than its happy path: a corrupt ledger line, a
sign slip in config, or a stale day boundary must not silently switch it
off.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")

import bot.daily_loss as daily_loss
from bot.daily_loss import COLOMBO, daily_limit_reason, realized_pl_today

TODAY = date(2026, 9, 8)


def write_ledger(tmp: Path, rows: list[dict]) -> None:
    path = tmp / "logs" / "live2_m3"
    path.mkdir(parents=True, exist_ok=True)
    with (path / "trade_history.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def trade(ticket: int, profit: float, when: datetime) -> dict:
    return {"ticket": ticket, "profit": profit, "close_time": when.isoformat()}


def at(hour: int, minute: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=COLOMBO)


def check(label: str, cond: bool) -> None:
    assert cond, f"FAILED: {label}"
    print(f"  OK  {label}")


def main() -> None:
    print("daily_loss logic checks")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        daily_loss.PROJECT_ROOT = tmp  # redirect the ledger lookup

        # No ledger at all -> no trades, no block.
        check("missing ledger -> $0.00 and no block",
              realized_pl_today("logs", "live2_m3", TODAY) == 0.0
              and daily_limit_reason("logs", "live2_m3", 50.0, TODAY) is None)

        write_ledger(tmp, [
            trade(1, -20.0, at(9)),
            trade(2, +10.0, at(10)),
            trade(3, -15.0, at(11)),
            # Yesterday must not count.
            trade(4, -500.0, at(12, day=TODAY - timedelta(days=1))),
            # Tomorrow must not count either.
            trade(5, -500.0, at(12, day=TODAY + timedelta(days=1))),
        ])

        check("sums only TODAY's Colombo trades (-20 +10 -15 = -25)",
              abs(realized_pl_today("logs", "live2_m3", TODAY) - (-25.0)) < 1e-9)
        check("under the limit -> trading continues",
              daily_limit_reason("logs", "live2_m3", 50.0, TODAY) is None)
        check("over the limit -> blocked",
              daily_limit_reason("logs", "live2_m3", 25.0, TODAY) is not None)
        check("exactly AT the limit -> blocked (>= is the safe side)",
              daily_limit_reason("logs", "live2_m3", 25.0, TODAY) is not None)

        # A sign slip in config must not disable the rule.
        check("a negative limit means the same as a positive one",
              daily_limit_reason("logs", "live2_m3", -25.0, TODAY) is not None)

        # Off switches.
        check("limit None -> rule inert",
              daily_limit_reason("logs", "live2_m3", None, TODAY) is None)
        check("limit 0 -> rule inert (not 'block everything')",
              daily_limit_reason("logs", "live2_m3", 0.0, TODAY) is None)

        # A profitable day never blocks, however large the swings.
        write_ledger(tmp, [trade(1, -300.0, at(9)), trade(2, +400.0, at(10))])
        check("net-positive day never blocks, despite a -$300 trade",
              daily_limit_reason("logs", "live2_m3", 50.0, TODAY) is None)

        # Corrupt / partial lines must be skipped, not crash or zero the total.
        path = tmp / "logs" / "live2_m3" / "trade_history.jsonl"
        with path.open("a") as f:
            f.write("{ this is not json\n")
            f.write(json.dumps({"ticket": 9, "profit": -10.0}) + "\n")   # no close_time
            f.write(json.dumps({"ticket": 10, "close_time": at(11).isoformat()}) + "\n")  # no profit
            f.write("\n")
        check("corrupt and incomplete lines are skipped, good ones still counted",
              abs(realized_pl_today("logs", "live2_m3", TODAY) - 100.0) < 1e-9)

        # A UTC-stamped close time must be converted, not compared raw.
        # 2026-09-08 20:00 UTC is 2026-09-09 01:30 in Colombo -> NOT today.
        write_ledger(tmp, [
            trade(1, -99.0, datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)),
        ])
        check("close_time in UTC is converted to the Colombo day",
              realized_pl_today("logs", "live2_m3", TODAY) == 0.0)

    print("ALL LOGIC CHECKS PASSED")


if __name__ == "__main__":
    main()
