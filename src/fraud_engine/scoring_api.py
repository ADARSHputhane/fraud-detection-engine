"""Read-side routes: what the pipeline computed for an event."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from .db import Database, FeatureRecord
from .features import FEATURE_NAMES
from .schemas import StoredFeatures

router = APIRouter()


def ordered(vector: dict) -> dict:
    """Catalogue order: Postgres JSONB does not preserve key order."""
    return {name: vector.get(name) for name in FEATURE_NAMES}


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/features/{event_id}", response_model=StoredFeatures)
async def get_features(event_id: UUID, request: Request) -> StoredFeatures:
    """The full feature vector computed for an event."""
    async with _db(request).sessionmaker() as session:
        record = await session.get(FeatureRecord, event_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "no features for this event yet — is the worker running?"
        )
    return StoredFeatures(
        event_id=record.event_id,
        feature_version=record.feature_version,
        computed_at=record.computed_at,
        features=ordered(record.vector),
    )
