"""Offline feature computation: the live FeatureEngine run over a file of events.

Rule evaluation and model training read features produced here, so they see exactly what the
worker computes in production. There is no second, "offline" implementation to drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import uuid4

from redis.asyncio import Redis

from .features import FeatureEngine


async def build_features(events: Iterable[dict], redis: Redis) -> list[dict]:
    """Compute vectors for time-ordered events on a fresh, throwaway key prefix."""
    engine = FeatureEngine(redis, prefix=f"offline:{uuid4().hex[:8]}")
    try:
        return [{"event_id": e["event_id"], "features": await engine.compute(e)} for e in events]
    finally:
        await engine.reset()


def join_labels(rows: list[dict], labels: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each feature row with its ground-truth label (rows without one are dropped)."""
    by_id = {lbl["event_id"]: lbl for lbl in labels}
    return [(r["features"], by_id[r["event_id"]]) for r in rows if r["event_id"] in by_id]
