"""Latency and throughput read back from the audit log."""

from __future__ import annotations

import statistics
from datetime import datetime

from sqlalchemy import select

from .db import Database, DecisionRecord


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    if len(values) == 1:
        v = round(values[0], 3)
        return {"p50": v, "p95": v, "p99": v, "max": v}
    qs = statistics.quantiles(values, n=100)
    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(qs[94], 3),
        "p99": round(qs[98], 3),
        "max": round(max(values), 3),
    }


async def latency_report(db: Database, since: datetime | None = None) -> dict:
    query = select(DecisionRecord.decided_at, DecisionRecord.latency_ms, DecisionRecord.stage_ms)
    if since is not None:
        query = query.where(DecisionRecord.decided_at >= since)
    async with db.sessionmaker() as session:
        rows = (await session.execute(query)).all()
    if not rows:
        return {"decisions": 0}
    first, last = min(r.decided_at for r in rows), max(r.decided_at for r in rows)
    span = (last - first).total_seconds()
    queue = [r.stage_ms.get("queue", 0.0) for r in rows]
    processing = [r.latency_ms for r in rows]
    return {
        "decisions": len(rows),
        "window_seconds": round(span, 2),
        "decisions_per_second": round(len(rows) / span, 1) if span else None,
        "processing_ms": percentiles(processing),
        "queue_ms": percentiles(queue),
        "end_to_end_ms": percentiles([q + p for q, p in zip(queue, processing, strict=True)]),
        "stages_ms": {
            stage: percentiles([r.stage_ms[stage] for r in rows if stage in r.stage_ms])
            for stage in ("features", "rules", "ml", "decision")
        },
    }
