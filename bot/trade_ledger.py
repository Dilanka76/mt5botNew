"""Append-only local record of this bot's own closed trades — the
permanent data source behind the analytics dashboard, independent of
however far back MT5's own history happens to be queryable.

Deliberately pure/MT5-free, like bot/status_writer.py: main.py is the only
place that touches MT5, and calls append_new_trades() here with the same
plain-dict trades it already builds for status.json's "recent_closed_trades"
(bot/mt5_connector.py's MT5Connector.get_recent_closed_trades()) — so this
module never needs to know MT5 objects exist.

Deduplicated by "ticket": main.py calls this every heartbeat (60s) with
whatever the last few closed trades currently are, so the same trade is
seen repeatedly — only genuinely new tickets get appended.
"""
from __future__ import annotations

import json
from pathlib import Path

from bot.config import PROJECT_ROOT


def trade_ledger_path(log_dir: str, account: str) -> Path:
    return PROJECT_ROOT / log_dir / account / "trade_history.jsonl"


def append_new_trades(path: Path, trades: list[dict]) -> int:
    """Appends any trade in `trades` whose "ticket" isn't already present
    in the ledger. Returns how many were newly appended (0 most calls,
    since trades close far less often than the 60s heartbeat that calls
    this). Reads the whole existing ledger to dedupe — trivially fast even
    after months of trades (a personal-scale JSONL file, not a firehose)."""
    if not trades:
        return 0

    existing_tickets: set[int] = set()
    if path.exists():
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing_tickets.add(json.loads(line)["ticket"])
            except (json.JSONDecodeError, KeyError):
                continue

    new_trades = [t for t in trades if t["ticket"] not in existing_tickets]
    if not new_trades:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for t in new_trades:
            f.write(json.dumps(t) + "\n")

    return len(new_trades)
