"""Redis Streams publisher and consumer.

The worker reads `events:incoming` through a consumer group, so each event is handed to one
worker even with several running, and an event a worker took but never acknowledged (it
crashed mid-batch) is still pending and gets processed again.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from redis.asyncio import Redis
from redis.exceptions import ResponseError

Entry = tuple[str, dict[str, str]]


def _s(v: object) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


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


class EventConsumer:
    def __init__(self, redis: Redis, name: str, group: str, consumer: str) -> None:
        self.redis = redis
        self.name = name
        self.group = group
        self.consumer = consumer

    async def ensure_group(self) -> None:
        """Create the consumer group if needed, starting from the beginning of the stream so
        events published before the first worker started are still processed."""
        try:
            await self.redis.xgroup_create(self.name, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self, count: int, block_ms: int | None = None, pending: bool = False
    ) -> list[Entry]:
        """Read new entries, or with `pending=True` the ones this consumer took earlier but
        never acknowledged."""
        resp = await self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.name: "0" if pending else ">"},
            count=count,
            block=None if pending else block_ms,
        )
        entries: list[Entry] = []
        for _stream, items in resp or []:
            for entry_id, fields in items:
                # A pending entry trimmed from the stream comes back with no fields.
                entries.append((_s(entry_id), {_s(k): _s(v) for k, v in (fields or {}).items()}))
        return entries

    async def ack(self, ids: list[str]) -> None:
        if ids:
            await self.redis.xack(self.name, self.group, *ids)

    async def dead_letter(self, stream: str, entry_id: str, fields: dict, error: str) -> None:
        await self.redis.xadd(
            stream, {**fields, "source_id": entry_id, "error": error[:500]}, maxlen=10_000
        )
