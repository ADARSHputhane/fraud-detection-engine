"""Read-side routes: what the pipeline computed for an event."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from .db import Database, DecisionRecord, FeatureRecord
from .decision import score_band
from .features import FEATURE_NAMES
from .ml import ModelRegistry
from .rules import RulesEngine
from .schemas import (
    RuleOut,
    RulesetOut,
    ScoreRequest,
    ScoreResponse,
    StoredDecision,
    StoredFeatures,
)

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


def _models(request: Request) -> ModelRegistry:
    return request.app.state.models


@router.post("/score", response_model=ScoreResponse)
async def score(body: ScoreRequest, request: Request) -> ScoreResponse:
    """Score a feature vector with the current model: calibrated 0-100 risk plus the five
    features that pushed it most (SHAP values, in log-odds)."""
    unknown = sorted(set(body.features) - set(FEATURE_NAMES))
    if unknown:
        raise HTTPException(422, f"unknown features: {unknown}")
    registry = _models(request)
    result = registry.score(body.features)
    if result is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no model has been promoted yet — run `fraud-engine train --promote`",
        )
    settings = request.app.state.settings
    return ScoreResponse(
        score=result.score,
        band=score_band(result.score, settings.review_threshold, settings.deny_threshold),
        model_version=result.model_version,
        explanation=result.explanation,
    )


@router.get("/models/current")
async def current_model(request: Request) -> dict[str, Any]:
    """Metadata of the model in use: version, features, thresholds, test metrics."""
    registry = _models(request)
    registry.maybe_reload()
    if registry.current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, registry.last_error or "no model promoted")
    return registry.current.metadata
