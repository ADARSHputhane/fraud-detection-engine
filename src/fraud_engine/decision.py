"""Turn the stage outputs into one decision: ALLOW, REVIEW or DENY."""

from __future__ import annotations

from typing import Literal

from .rules import RuleResult

Decision = Literal["ALLOW", "REVIEW", "DENY"]


def decide(rules: RuleResult) -> tuple[Decision, str]:
    """Return (decision, decided_by). The strongest, then heaviest, fired rule wins."""
    if rules.action:
        top = max(
            (h for h in rules.hits if h.action == rules.action), key=lambda h: (h.weight, h.id)
        )
        return rules.action, f"rule:{top.id}"
    return "ALLOW", "default"
