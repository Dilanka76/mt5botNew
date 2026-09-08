"""disable_decision_log() must silence decisions without writing a file.

    python3 tests/test_decision_log_disable.py

Offline replays drive the real engine, so they call log_decision on every
simulated trade. On 2026-09-09 a parallel sweep died with

    RuntimeError: setup_logging() must be called before log_decision()

because worker processes are fresh interpreters on Windows and had no
logging at all. The obvious fix -- call setup_logging in each worker --
would point several RotatingFileHandlers in different processes at one
decisions.jsonl, and on Windows a locked file during rotation raises
rather than waits. So the sweep writes nothing instead.

What must hold:
  - log_decision does not raise once disabled
  - and creates no file
  - setup_logging still works normally afterwards, so disabling in one
    process cannot leak into a real run
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")

# bot.config imports yaml and python-dotenv; neither is needed to exercise
# the logger, and neither is installed on a plain dev machine. Same stubbing
# convention the MetaTrader5 tests use, so this runs anywhere.
for _name, _attrs in (("yaml", {"safe_load": lambda *a, **k: {}}),
                      ("dotenv", {"load_dotenv": lambda *a, **k: None})):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        for _k, _v in _attrs.items():
            setattr(_stub, _k, _v)
        sys.modules[_name] = _stub

from bot.logging_setup.logger import disable_decision_log, log_decision, setup_logging

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  {'OK  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> None:
    print("disable_decision_log")

    # Without any setup, log_decision must still raise -- that guard is
    # what stops a real run from silently losing its decision history.
    import bot.logging_setup.logger as mod
    mod._decision_logger = None
    try:
        log_decision("XAUUSDp", "trade_entered", "no setup")
        raised = False
    except RuntimeError:
        raised = True
    check("still raises when nothing has been configured", raised)

    disable_decision_log()
    try:
        for _ in range(50):
            log_decision("XAUUSDp", "trade_entered", "replayed", ticket=1)
        ok = True
    except Exception as exc:                      # noqa: BLE001 - reporting it is the point
        ok = False
        print(f"        raised: {exc!r}")
    check("does not raise once disabled", ok)
    check("writes to a NullHandler, so nothing reaches disk",
          all(h.__class__.__name__ == "NullHandler" for h in mod._decision_logger.handlers))
    check("and does not propagate to a parent that might have a file handler",
          mod._decision_logger.propagate is False)

    # A real setup afterwards must still produce a real file, or disabling
    # in a sweep could quietly disarm a live account's decision log.
    from bot.config import LoggingConfig
    with tempfile.TemporaryDirectory() as tmp:
        setup_logging(LoggingConfig(log_dir=tmp, level="INFO"), "unit-test")
        log_decision("XAUUSDp", "trade_entered", "real one")
        for h in mod._decision_logger.handlers:
            h.flush()
        written = Path(tmp) / "unit-test" / "decisions.jsonl"
        check("setup_logging still writes decisions after a disable",
              written.exists() and "real one" in written.read_text(encoding="utf-8"))

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
