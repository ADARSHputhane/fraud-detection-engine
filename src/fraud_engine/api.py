"""Ingest API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from . import metrics
from .db import Database, EventRecord
from .schemas import (
    BatchAccepted,
    EventAccepted,
    EventBatch,
    InstrumentEvent,
    PaymentEvent,
    StoredEvent,
)
from .stream import EventStream

router = APIRouter()

AnyEvent = PaymentEvent | InstrumentEvent


def _db(request: Request) -> Database:
    return request.app.state.db


def _stream(request: Request) -> EventStream:
    return request.app.state.stream


def _record(event: AnyEvent, payload: dict) -> EventRecord:
    return EventRecord(
        event_id=event.event_id,
        event_type=event.event_type,
        user_id=event.user_id,
        card_id=event.card_id,
        device_id=event.device_id,
        occurred_at=event.timestamp,
        payload=payload,
    )


async def _ingest_one(request: Request, event: AnyEvent) -> EventAccepted:
    db, stream = _db(request), _stream(request)
    payload = event.model_dump(mode="json")

    async with db.sessionmaker() as session:
        session.add(_record(event, payload))
        try:
            await session.commit()
        except IntegrityError:
            metrics.INGESTED.labels(event.event_type, "duplicate").inc()
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"event {event.event_id} was already received"
            ) from None

    try:
        stream_id = await stream.publish(event.event_type, str(event.event_id), payload)
    except RedisError:
        # Undo the insert so the client's retry isn't rejected as a duplicate of an event
        # that never reached the pipeline.
        async with db.sessionmaker() as session:
            await session.execute(delete(EventRecord).where(EventRecord.event_id == event.event_id))
            await session.commit()
        metrics.INGESTED.labels(event.event_type, "unavailable").inc()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "event stream unavailable, retry"
        ) from None

    metrics.INGESTED.labels(event.event_type, "accepted").inc()
    return EventAccepted(event_id=event.event_id, stream_id=stream_id)


@router.post("/events/payment", status_code=status.HTTP_202_ACCEPTED, response_model=EventAccepted)
async def ingest_payment(event: PaymentEvent, request: Request) -> EventAccepted:
    """Accept a payment attempt for scoring."""
    return await _ingest_one(request, event)


@router.post(
    "/events/instrument", status_code=status.HTTP_202_ACCEPTED, response_model=EventAccepted
)
async def ingest_instrument(event: InstrumentEvent, request: Request) -> EventAccepted:
    """Accept a card being added, updated or removed on an account."""
    return await _ingest_one(request, event)


@router.post("/events/batch", status_code=status.HTTP_202_ACCEPTED, response_model=BatchAccepted)
async def ingest_batch(batch: EventBatch, request: Request) -> BatchAccepted:
    """Accept up to 1,000 mixed events in order. Already-seen event_ids are skipped and
    reported back; the whole batch is rejected with 422 if any event is invalid."""
    db, stream = _db(request), _stream(request)

    # De-duplicate inside the batch first, keeping the first occurrence and the order.
    seen: set[UUID] = set()
    duplicates: list[UUID] = []
    unique: list[AnyEvent] = []
    for ev in batch.events:
        if ev.event_id in seen:
            duplicates.append(ev.event_id)
        else:
            seen.add(ev.event_id)
            unique.append(ev)

    async with db.sessionmaker() as session:
        existing = set(
            (
                await session.scalars(
                    select(EventRecord.event_id).where(EventRecord.event_id.in_(seen))
                )
            ).all()
        )
        new_events = [ev for ev in unique if ev.event_id not in existing]
        duplicates.extend(ev.event_id for ev in unique if ev.event_id in existing)

        payloads = [ev.model_dump(mode="json") for ev in new_events]
        session.add_all(_record(ev, p) for ev, p in zip(new_events, payloads, strict=True))
        try:
            await session.commit()
        except IntegrityError:
            # Another request inserted one of these ids between our check and our insert.
            raise HTTPException(
                status.HTTP_409_CONFLICT, "concurrent duplicate detected, retry the batch"
            ) from None

    if new_events:
        try:
            await stream.publish_many(
                (ev.event_type, str(ev.event_id), p)
                for ev, p in zip(new_events, payloads, strict=True)
            )
        except RedisError:
            async with db.sessionmaker() as session:
                await session.execute(
                    delete(EventRecord).where(
                        EventRecord.event_id.in_([ev.event_id for ev in new_events])
                    )
                )
                await session.commit()
            for ev in new_events:
                metrics.INGESTED.labels(ev.event_type, "unavailable").inc()
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "event stream unavailable, retry"
            ) from None

    for ev in new_events:
        metrics.INGESTED.labels(ev.event_type, "accepted").inc()
    for _ in duplicates:
        metrics.INGESTED.labels("unknown", "duplicate").inc()
    return BatchAccepted(accepted=len(new_events), duplicates=duplicates)


@router.get("/events/{event_id}", response_model=StoredEvent)
async def get_event(event_id: UUID, request: Request) -> StoredEvent:
    """Fetch a stored event — handy for debugging the pipeline."""
    async with _db(request).sessionmaker() as session:
        record = await session.get(EventRecord, event_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    return StoredEvent(
        event_id=record.event_id,
        event_type=record.event_type,
        received_at=record.received_at,
        payload=record.payload,
    )


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    checks = {
        "postgres": await _db(request).ping(),
        "redis": await _stream(request).ping(),
    }
    healthy = all(checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", **checks},
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
