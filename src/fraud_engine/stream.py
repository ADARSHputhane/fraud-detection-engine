"""Redis Streams publisher.

Downstream consumers (the Milestone 2 feature engine) read `events:incoming` with consumer
groups, so each event is processed once even with several workers running.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from redis.asyncio import Redis


class EventStream:
    def __init__(self, redis: Redis, name: str, maxlen: int) -> None:
        self.redis = redis
        self.name = name
        self.maxlen = maxlen

    @staticmethod
    def _fields(event_type: str, event_id: str, payload: dict) -> dict[str, str]:
        # Stream entries are flat string maps; keep routing fields top-level and the full
        # event as JSON so consumers don't need a second lookup.
        return {"event_type": event_type, "event_id": event_id, "data": json.dumps(payload)}

    async def publish(self, event_type: str, event_id: str, payload: dict) -> str:
        stream_id = await self.redis.xadd(
            self.name,
            self._fields(event_type, event_id, payload),
            maxlen=self.maxlen,
            approximate=True,
        )
        return stream_id.decode() if isinstance(stream_id, bytes) else stream_id

    async def publish_many(self, items: Iterable[tuple[str, str, dict]]) -> list[str]:
        """Publish in one round trip with a pipeline — used by the batch endpoint."""
        async with self.redis.pipeline(transaction=False) as pipe:
            for event_type, event_id, payload in items:
                pipe.xadd(
                    self.name,
                    self._fields(event_type, event_id, payload),
                    maxlen=self.maxlen,
                    approximate=True,
                )
            ids = await pipe.execute()
        return [i.decode() if isinstance(i, bytes) else i for i in ids]

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except Exception:
            return False
