"""Read-side routes: what the pipeline computed for an event."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from .db import Database, DecisionRecord, FeatureRecord
from .features import FEATURE_NAMES
from .rules import RulesEngine
from .schemas import RuleOut, RulesetOut, StoredDecision, StoredFeatures

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


@router.get("/decisions/{event_id}", response_model=StoredDecision)
async def get_decision(event_id: UUID, request: Request) -> StoredDecision:
    """The audit-log entry for an event: decision, fired rules with reasons, timings."""
    async with _db(request).sessionmaker() as session:
        record = await session.get(DecisionRecord, event_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no decision for this event yet")
    return StoredDecision.model_validate(record, from_attributes=True)


@router.get("/rules", response_model=RulesetOut)
async def get_rules(request: Request) -> RulesetOut:
    """The ruleset currently in force (reloaded from disk if the file changed)."""
    engine: RulesEngine = request.app.state.rules
    engine.maybe_reload()
    rs = engine.ruleset
    return RulesetOut(
        version=rs.version,
        rules=[
            RuleOut(
                id=r.id,
                name=r.name,
                condition=r.condition,
                action=r.action,
                weight=r.weight,
                reason=r.reason,
            )
            for r in rs.rules
        ],
        last_reload_error=engine.last_error,
    )
