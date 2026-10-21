"""Turn the stage outputs into one decision: ALLOW, REVIEW or DENY.

Override hierarchy, strongest first:

1. A rule that says DENY always wins. Rules encode known attacks; the model can't overrule them.
2. A model score above the DENY threshold (default 75) denies.
3. A rule that says REVIEW, or a score at or above the REVIEW threshold (default 40), sends
   the event to an analyst. A low model score never cancels a rule's REVIEW.
4. Otherwise ALLOW.
"""

from __future__ import annotations

from typing import Literal

from .ml import MLScore
from .rules import RuleResult

Decision = Literal["ALLOW", "REVIEW", "DENY"]

REVIEW_THRESHOLD = 40.0
DENY_THRESHOLD = 75.0


def score_band(
    score: float, review_at: float = REVIEW_THRESHOLD, deny_above: float = DENY_THRESHOLD
) -> Decision:
    if score > deny_above:
        return "DENY"
    if score >= review_at:
        return "REVIEW"
    return "ALLOW"


def decide(
    rules: RuleResult,
    ml: MLScore | None = None,
    *,
    review_at: float = REVIEW_THRESHOLD,
    deny_above: float = DENY_THRESHOLD,
) -> tuple[Decision, str]:
    """Return (decision, decided_by), where decided_by is "rule:<id>", "model" or "default"."""
    top = None
    if rules.action:
        top = max(
            (h for h in rules.hits if h.action == rules.action), key=lambda h: (h.weight, h.id)
        )
    if rules.action == "DENY":
        return "DENY", f"rule:{top.id}"
    band = score_band(ml.score, review_at, deny_above) if ml else "ALLOW"
    if band == "DENY":
        return "DENY", "model"
    # Credit the rule when both ask for review: its reason is easier to act on.
    if rules.action == "REVIEW":
        return "REVIEW", f"rule:{top.id}"
    if band == "REVIEW":
        return "REVIEW", "model"
    return "ALLOW", "default"
