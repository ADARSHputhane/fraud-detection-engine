"""Stream worker: reads events:incoming, runs each event through the pipeline, and writes
the results to Postgres.

Events within a batch are processed one at a time, in stream order, because each one updates
the state the next one's features are computed from. Persisting is batched: one transaction
per batch, then one XACK. If the database write fails the batch is not acknowledged, so it is
retried; the feature engine returns the cached vector for events it already processed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from . import metrics
from .db import Database, DecisionRecord, FeatureRecord, RulesetRecord
from .features import FEATURE_VERSION
from .pipeline import Pipeline, PipelineResult
from .stream import Entry, EventConsumer

log = logging.getLogger(__name__)

PERSIST_RETRIES = 5


def entry_time_ms(entry_id: str) -> float:
    """Redis stream ids start with the millisecond timestamp of the XADD."""
    return float(entry_id.split("-", 1)[0])


def decision_row(r: PipelineResult, review_at: float, deny_above: float) -> dict:
    return {
        "event_id": UUID(r.event_id),
        "event_type": r.event["event_type"],
        "user_id": r.event["user_id"],
        "occurred_at": datetime.fromisoformat(r.event["timestamp"]),
        "final_decision": r.decision,
        "decided_by": r.decided_by,
        "rules_fired": [h.to_dict() for h in r.rules.hits],
        "rule_score": r.rules.score,
        "ruleset_version": r.rules.ruleset_version,
        "ml_score": r.ml.score if r.ml else None,
        "model_version": r.ml.model_version if r.ml else None,
        "shap_top5": r.ml.explanation if r.ml else None,
        "thresholds": {"review_at": review_at, "deny_above": deny_above},
        "latency_ms": r.latency_ms,
        "stage_ms": {**r.timings_ms, "queue": r.queue_ms},
    }


class Worker:
    def __init__(
        self,
        *,
        db: Database,
        consumer: EventConsumer,
        pipeline: Pipeline,
        deadletter_stream: str,
        batch_size: int = 100,
        block_ms: int = 1_000,
        latency_budget_ms: float = 100.0,
    ) -> None:
        self.db = db
        self.consumer = consumer
        self.pipeline = pipeline
        self.deadletter_stream = deadletter_stream
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.latency_budget_ms = latency_budget_ms
        self.processed = 0
        self.failed = 0
        self._saved_rulesets: set[str] = set()
        self._model_version: str | None = None

    async def handle(self, entries: list[Entry]) -> list[PipelineResult]:
        results: list[PipelineResult] = []
        for entry_id, fields in entries:
            started_ms = time.time() * 1000
            try:
                event = json.loads(fields["data"])
                result = await self.pipeline.process(event)
            except Exception as exc:  # one bad event must not stall the whole stream
                self.failed += 1
                metrics.FAILED.inc()
                log.exception("failed to process stream entry %s", entry_id)
                await self.consumer.dead_letter(
                    self.deadletter_stream, entry_id, fields, f"{type(exc).__name__}: {exc}"
                )
                continue
            result.queue_ms = round(max(started_ms - entry_time_ms(entry_id), 0.0), 3)
            results.append(result)
            self._observe(result)
        await self._persist_with_retry(results)
        await self.consumer.ack([entry_id for entry_id, _ in entries])
        self.processed += len(results)
        return results

    def _observe(self, r: PipelineResult) -> None:
        metrics.PROCESSED.labels(r.event["event_type"]).inc()
        metrics.DECISIONS.labels(r.decision, r.decided_by.split(":", 1)[0]).inc()
        for hit in r.rules.hits:
            metrics.RULE_HITS.labels(hit.id).inc()
        for stage, ms in r.timings_ms.items():
            metrics.STAGE_SECONDS.labels(stage).observe(ms / 1000)
        end_to_end = r.queue_ms + r.latency_ms
        metrics.END_TO_END_SECONDS.observe(end_to_end / 1000)
        if end_to_end > self.latency_budget_ms:
            metrics.OVER_BUDGET.inc()
        version = r.ml.model_version if r.ml else None
        if version != self._model_version:
            if self._model_version:
                metrics.MODEL_IN_USE.labels(self._model_version).set(0)
            if version:
                metrics.MODEL_IN_USE.labels(version).set(1)
            self._model_version = version

    async def _persist_with_retry(self, results: list[PipelineResult]) -> None:
        for attempt in range(1, PERSIST_RETRIES + 1):
            try:
                with metrics.PERSIST_SECONDS.time():
                    await self.persist(results)
                return
            except SQLAlchemyError:
                if attempt == PERSIST_RETRIES:
                    raise  # crash; the batch stays pending and is retried on restart
                log.warning("persist failed (attempt %d), retrying", attempt, exc_info=True)
                await asyncio.sleep(0.5 * 2**attempt)

    async def persist(self, results: list[PipelineResult]) -> None:
        if not results:
            return
        p = self.pipeline
        features = [
            {"event_id": UUID(r.event_id), "feature_version": FEATURE_VERSION, "vector": r.features}
            for r in results
        ]
        decisions = [decision_row(r, p.review_at, p.deny_above) for r in results]
        new_rulesets = [
            {"version": v, "source": p.rules.history[v].source}
            for v in {r.rules.ruleset_version for r in results} - self._saved_rulesets
        ]
        async with self.db.sessionmaker() as session:
            if new_rulesets:
                await session.execute(self.db.insert_ignore(RulesetRecord, new_rulesets))
            await session.execute(self.db.insert_ignore(FeatureRecord, features))
            await session.execute(self.db.insert_ignore(DecisionRecord, decisions))
            await session.commit()
        self._saved_rulesets.update(r["version"] for r in new_rulesets)

    async def run_once(self) -> int:
        """Process one batch of new entries; returns how many entries were read."""
        entries = await self.consumer.read(self.batch_size, self.block_ms)
        if entries:
            await self.handle(entries)
        return len(entries)

    async def recover_pending(self) -> None:
        """Finish anything this consumer took before a crash or restart."""
        while entries := await self.consumer.read(self.batch_size, pending=True):
            await self.handle(entries)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        await self.consumer.ensure_group()
        await self.recover_pending()
        log.info("worker %s consuming %s", self.consumer.consumer, self.consumer.name)
        while not stop.is_set():
            await self.run_once()
