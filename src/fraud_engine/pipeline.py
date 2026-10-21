"""The scoring pipeline: one event in, everything the engine concluded about it out.

Stages run in order and each one is timed, so the audit log can show where the latency
budget went.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .features import FeatureEngine


@dataclass
class PipelineResult:
    event: dict
    features: dict[str, Any]
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        return str(self.event["event_id"])


class Pipeline:
    def __init__(self, features: FeatureEngine) -> None:
        self.features = features

    async def process(self, event: dict) -> PipelineResult:
        t0 = perf_counter()
        vector = await self.features.compute(event)
        t1 = perf_counter()
        return PipelineResult(
            event=event,
            features=vector,
            timings_ms={"features": round((t1 - t0) * 1000, 3)},
        )
