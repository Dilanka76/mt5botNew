"""EMA5-touch-only strategy variant.

Identical to EMAScalpEngine (state_machine.py) — same cross detection, same
EMA5-touch waiting/invalidation logic, same close-then-reevaluate ordering,
same TP-fill detection, same session gating, same position sizing/execution
— except the $5 gap threshold is not used to decide immediate-vs-wait.
Every single cross always waits for price to touch EMA5 before entering.

Subclasses EMAScalpEngine and overrides only the entry decision, so all the
shared logic stays defined once (in state_machine.py) and this variant
can't drift out of sync with it.
"""
from __future__ import annotations

from bot.logging_setup.logger import log_decision
from bot.strategy.cross_detector import CrossEvent
from bot.strategy.state_machine import EMAScalpEngine, PendingSetup, TradeState


class EMA5OnlyEngine(EMAScalpEngine):
    def _decide_entry(self, event: CrossEvent, gap: float) -> None:
        self.pending = PendingSetup(direction=event.direction, cross_event=event, gap=gap)
        self.state = TradeState.PENDING_ENTRY
        log_decision(
            self.config.symbol,
            "setup_pending",
            f"{event.direction.value} cross, gap={gap:.2f} (gap check disabled in this variant), waiting for EMA5 touch",
        )

    def _check_early_entry(self, tick) -> None:
        # This variant always waits for an EMA5 touch, gap check disabled
        # entirely (see _decide_entry above) — the early-entry idea only
        # ever makes sense for the gap-threshold immediate-entry path, so
        # it's a deliberate no-op here regardless of early_entry_threshold_usd.
        return
