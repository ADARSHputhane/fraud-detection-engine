"""The scoring pipeline: one event in, everything the engine concluded about it out.

Stages run in order and each one is timed, so the audit log can show where the latency
budget went.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .decision import DENY_THRESHOLD, REVIEW_THRESHOLD, Decision, decide
from .features import FeatureEngine
from .ml import MLScore, ModelRegistry
from .rules import RuleResult, RulesEngine


@dataclass
class PipelineResult:
    event: dict
    features: dict[str, Any]
    rules: RuleResult
    ml: MLScore | None
    decision: Decision
    decided_by: str
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        return str(self.event["event_id"])

    @property
    def latency_ms(self) -> float:
        return round(sum(self.timings_ms.values()), 3)


def _ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 3)


class Pipeline:
    def __init__(
        self,
        features: FeatureEngine,
        rules: RulesEngine | None = None,
        models: ModelRegistry | None = None,
        *,
        review_at: float = REVIEW_THRESHOLD,
        deny_above: float = DENY_THRESHOLD,
    ) -> None:
        self.features = features
        self.rules = rules or RulesEngine()
        self.models = models
        self.review_at = review_at
        self.deny_above = deny_above

    async def process(self, event: dict) -> PipelineResult:
        t0 = perf_counter()
        vector = await self.features.compute(event)
        t1 = perf_counter()
        rule_result = self.rules.evaluate(vector)
        t2 = perf_counter()
        # The model scores every event, even ones a rule already denies: the audit log then
        # shows where rules and model disagree, which is what tuning both depends on.
        ml = self.models.score(vector) if self.models else None
        t3 = perf_counter()
        decision, decided_by = decide(
            rule_result, ml, review_at=self.review_at, deny_above=self.deny_above
        )
        t4 = perf_counter()
        return PipelineResult(
            event=event,
            features=vector,
            rules=rule_result,
            ml=ml,
            decision=decision,
            decided_by=decided_by,
            timings_ms={
                "features": _ms(t0, t1),
                "rules": _ms(t1, t2),
                "ml": _ms(t2, t3),
                "decision": _ms(t3, t4),
            },
        )
