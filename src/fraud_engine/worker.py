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
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .db import Database, DecisionRecord, FeatureRecord
from .features import FEATURE_VERSION
from .pipeline import Pipeline, PipelineResult
from .stream import Entry, EventConsumer

log = logging.getLogger(__name__)

PERSIST_RETRIES = 5


def _decision_row(r: PipelineResult) -> dict:
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
        "latency_ms": r.latency_ms,
        "stage_ms": r.timings_ms,
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
    ) -> None:
        self.db = db
        self.consumer = consumer
        self.pipeline = pipeline
        self.deadletter_stream = deadletter_stream
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.processed = 0
        self.failed = 0

    async def handle(self, entries: list[Entry]) -> list[PipelineResult]:
        results: list[PipelineResult] = []
        for entry_id, fields in entries:
            try:
                event = json.loads(fields["data"])
                results.append(await self.pipeline.process(event))
            except Exception as exc:  # one bad event must not stall the whole stream
                self.failed += 1
                log.exception("failed to process stream entry %s", entry_id)
                await self.consumer.dead_letter(
                    self.deadletter_stream, entry_id, fields, f"{type(exc).__name__}: {exc}"
                )
        await self._persist_with_retry(results)
        await self.consumer.ack([entry_id for entry_id, _ in entries])
        self.processed += len(results)
        return results

    async def _persist_with_retry(self, results: list[PipelineResult]) -> None:
        for attempt in range(1, PERSIST_RETRIES + 1):
            try:
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
        features = [
            {"event_id": UUID(r.event_id), "feature_version": FEATURE_VERSION, "vector": r.features}
            for r in results
        ]
        decisions = [_decision_row(r) for r in results]
        async with self.db.sessionmaker() as session:
            await session.execute(self.db.insert_ignore(FeatureRecord, features))
            await session.execute(self.db.insert_ignore(DecisionRecord, decisions))
            await session.commit()

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
