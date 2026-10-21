"""The scoring pipeline: one event in, everything the engine concluded about it out.

Stages run in order and each one is timed, so the audit log can show where the latency
budget went.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .decision import Decision, decide
from .features import FeatureEngine
from .rules import RuleResult, RulesEngine


@dataclass
class PipelineResult:
    event: dict
    features: dict[str, Any]
    rules: RuleResult
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
    def __init__(self, features: FeatureEngine, rules: RulesEngine | None = None) -> None:
        self.features = features
        self.rules = rules or RulesEngine()

    async def process(self, event: dict) -> PipelineResult:
        t0 = perf_counter()
        vector = await self.features.compute(event)
        t1 = perf_counter()
        rule_result = self.rules.evaluate(vector)
        t2 = perf_counter()
        decision, decided_by = decide(rule_result)
        t3 = perf_counter()
        return PipelineResult(
            event=event,
            features=vector,
            rules=rule_result,
            decision=decision,
            decided_by=decided_by,
            timings_ms={"features": _ms(t0, t1), "rules": _ms(t1, t2), "decision": _ms(t2, t3)},
        )
