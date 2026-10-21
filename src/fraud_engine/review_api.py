"""Analyst routes: the review queue, the case view, labels and dashboard statistics.

The case view never returns synthetic ground-truth labels: showing the analyst the answer
would make triage meaningless. Only the stats page compares decisions with ground truth.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import Integer, and_, cast, delete, func, select
from sqlalchemy.orm import aliased

from .db import Database, DecisionRecord, EventRecord, FeatureRecord, LabelRecord
from .features import FEATURES
from .reporting import percentiles
from .schemas import (
    Case,
    DecisionPage,
    FeatureValue,
    HistoryItem,
    LabelIn,
    LabelOut,
    QueueItem,
    StoredDecision,
)

router = APIRouter()

ANALYST = "analyst"
BY_DAY_DAYS = 30
_VIEWS: dict[str, tuple[str, ...]] = {
    "review": ("REVIEW",),
    "deny": ("DENY",),
    "allow": ("ALLOW",),
    "flagged": ("REVIEW", "DENY"),
    "all": ("ALLOW", "REVIEW", "DENY"),
}


def _db(request: Request) -> Database:
    return request.app.state.db


def _analyst_join():
    label = aliased(LabelRecord)
    on = and_(label.event_id == DecisionRecord.event_id, label.source == ANALYST)
    return label, on


def _top_rule(rules_fired: list[dict]) -> dict | None:
    return (
        max(rules_fired, key=lambda h: (h["action"] == "DENY", h["weight"]))
        if rules_fired
        else None
    )


def _top_raiser(shap_top5: list[dict] | None) -> dict | None:
    """The feature that pushed the score up most: the model's main reason to flag."""
    return next((c for c in shap_top5 or [] if c["contribution"] > 0), None)


@router.get("/decisions", response_model=DecisionPage)
async def list_decisions(
    request: Request,
    view: Literal["review", "deny", "allow", "flagged", "all"] = "review",
    labelled: Literal["no", "yes", "any"] = "no",
    sort: Literal["score", "recent"] = "score",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> DecisionPage:
    """The review queue. By default: REVIEW decisions no analyst has labelled yet, riskiest
    first."""
    label, on = _analyst_join()
    where = [DecisionRecord.final_decision.in_(_VIEWS[view])]
    if labelled == "no":
        where.append(label.event_id.is_(None))
    elif labelled == "yes":
        where.append(label.event_id.is_not(None))
    order = (
        [DecisionRecord.ml_score.desc().nulls_last(), DecisionRecord.rule_score.desc()]
        if sort == "score"
        else []
    ) + [DecisionRecord.occurred_at.desc()]

    query = (
        select(DecisionRecord, EventRecord.payload, label.is_fraud)
        .join(EventRecord, EventRecord.event_id == DecisionRecord.event_id)
        .outerjoin(label, on)
        .where(*where)
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    )
    count = select(func.count()).select_from(DecisionRecord).outerjoin(label, on).where(*where)
    async with _db(request).sessionmaker() as session:
        rows = (await session.execute(query)).all()
        total = (await session.execute(count)).scalar_one()

    items = []
    for d, payload, analyst_label in rows:
        items.append(
            QueueItem(
                event_id=d.event_id,
                event_type=d.event_type,
                user_id=d.user_id,
                occurred_at=d.occurred_at,
                final_decision=d.final_decision,
                decided_by=d.decided_by,
                ml_score=d.ml_score,
                rule_score=d.rule_score,
                amount=payload.get("amount"),
                currency=payload.get("currency"),
                merchant_category=payload.get("merchant_category"),
                top_rule=_top_rule(d.rules_fired),
                top_feature=_top_raiser(d.shap_top5),
                analyst_label=analyst_label,
            )
        )
    return DecisionPage(total=total, items=items)


def _label_out(record: LabelRecord) -> LabelOut:
    return LabelOut.model_validate(record, from_attributes=True)


@router.get("/cases/{event_id}", response_model=Case)
async def get_case(event_id: UUID, request: Request) -> Case:
    """The event, its decision, every feature with its description, the analyst's label if
    any, and the user's ten previous events."""
    async with _db(request).sessionmaker() as session:
        decision = await session.get(DecisionRecord, event_id)
        if decision is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no decision for this event")
        event = await session.get(EventRecord, event_id)
        features = await session.get(FeatureRecord, event_id)
        label = await session.get(LabelRecord, (event_id, ANALYST))
        history_rows = (
            await session.execute(
                select(DecisionRecord, EventRecord.payload)
                .join(EventRecord, EventRecord.event_id == DecisionRecord.event_id)
                .where(
                    DecisionRecord.user_id == decision.user_id,
                    DecisionRecord.occurred_at < decision.occurred_at,
                )
                .order_by(DecisionRecord.occurred_at.desc())
                .limit(10)
            )
        ).all()

    vector = features.vector if features else {}
    return Case(
        event=event.payload if event else {},
        decision=StoredDecision.model_validate(decision, from_attributes=True),
        features=[
            FeatureValue(
                name=f.name,
                group=f.group,
                kind=f.kind,
                description=f.description,
                value=vector.get(f.name),
            )
            for f in FEATURES
        ],
        label=_label_out(label) if label else None,
        history=[
            HistoryItem(
                event_id=d.event_id,
                event_type=d.event_type,
                occurred_at=d.occurred_at,
                final_decision=d.final_decision,
                ml_score=d.ml_score,
                amount=p.get("amount"),
                merchant_category=p.get("merchant_category"),
                country=(p.get("geo") or {}).get("country"),
            )
            for d, p in history_rows
        ],
    )


@router.post("/labels", response_model=LabelOut, status_code=status.HTTP_201_CREATED)
async def put_label(body: LabelIn, request: Request) -> LabelOut:
    """Record (or change) an analyst's verdict. It is used the next time the model is
    retrained, in preference to any other label for the event."""
    db = _db(request)
    row = {
        "event_id": body.event_id,
        "source": ANALYST,
        "is_fraud": body.is_fraud,
        "analyst": body.analyst,
        "note": body.note,
        "labelled_at": func.now(),
    }
    async with db.sessionmaker() as session:
        if await session.get(DecisionRecord, body.event_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no decision for this event")
        await session.execute(db.upsert(LabelRecord, [row], ["event_id", "source"]))
        await session.commit()
        record = await session.get(LabelRecord, (body.event_id, ANALYST), populate_existing=True)
    return _label_out(record)


@router.delete("/labels/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_label(event_id: UUID, request: Request) -> None:
    """Undo an analyst's verdict (the event returns to the queue)."""
    async with _db(request).sessionmaker() as session:
        await session.execute(
            delete(LabelRecord).where(
                LabelRecord.event_id == event_id, LabelRecord.source == ANALYST
            )
        )
        await session.commit()


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Numbers for the dashboard's stats page."""
    db = _db(request)
    analyst, on_analyst = _analyst_join()
    truth = aliased(LabelRecord)
    on_truth = and_(truth.event_id == DecisionRecord.event_id, truth.source == "synthetic")
    bucket = cast(DecisionRecord.ml_score / 10, Integer)
    day = func.date(DecisionRecord.occurred_at)
    async with db.sessionmaker() as session:
        by_decision = dict(
            (
                await session.execute(
                    select(DecisionRecord.final_decision, func.count()).group_by(
                        DecisionRecord.final_decision
                    )
                )
            ).all()
        )
        by_source = Counter()
        for decided_by, n in (
            await session.execute(
                select(DecisionRecord.decided_by, func.count()).group_by(DecisionRecord.decided_by)
            )
        ).all():
            by_source[decided_by.split(":", 1)[0]] += n
        queue = (
            await session.execute(
                select(func.count())
                .select_from(DecisionRecord)
                .outerjoin(analyst, on_analyst)
                .where(DecisionRecord.final_decision == "REVIEW", analyst.event_id.is_(None))
            )
        ).scalar_one()
        analyst_rows = (
            await session.execute(
                select(DecisionRecord.final_decision, analyst.is_fraud, truth.is_fraud)
                .join(analyst, on_analyst)
                .outerjoin(truth, on_truth)
            )
        ).all()
        truth_rows = (
            await session.execute(
                select(DecisionRecord.final_decision, truth.is_fraud, func.count())
                .join(truth, on_truth)
                .group_by(DecisionRecord.final_decision, truth.is_fraud)
            )
        ).all()
        rule_rows = (
            await session.scalars(
                select(DecisionRecord.rules_fired).where(DecisionRecord.rule_score > 0)
            )
        ).all()
        histogram = dict(
            (
                await session.execute(
                    select(bucket, func.count())
                    .where(DecisionRecord.ml_score.is_not(None))
                    .group_by(bucket)
                )
            ).all()
        )
        # The last 30 days of event time (cards added long ago would stretch it to years).
        latest = (await session.execute(select(func.max(DecisionRecord.occurred_at)))).scalar()
        daily = []
        if latest is not None:
            daily = (
                await session.execute(
                    select(day, DecisionRecord.final_decision, func.count())
                    .where(DecisionRecord.occurred_at >= latest - timedelta(days=BY_DAY_DAYS))
                    .group_by(day, DecisionRecord.final_decision)
                )
            ).all()
        recent = (
            await session.execute(
                select(DecisionRecord.latency_ms, DecisionRecord.stage_ms)
                .order_by(DecisionRecord.decided_at.desc())
                .limit(5_000)
            )
        ).all()

    # Analysts: what they confirmed, and (demo only) how often they agree with ground truth.
    confirmed = Counter()
    labelled = Counter()
    agree = compared = 0
    for decision, analyst_fraud, truth_fraud in analyst_rows:
        labelled[decision] += 1
        confirmed[decision] += bool(analyst_fraud)
        if truth_fraud is not None:
            compared += 1
            agree += analyst_fraud == truth_fraud

    # Decisions against ground truth, where it exists.
    outcome = {(d, bool(f)): n for d, f, n in truth_rows}
    fraud_total = sum(n for (_, f), n in outcome.items() if f)
    legit_total = sum(n for (_, f), n in outcome.items() if not f)
    flagged_fraud = sum(outcome.get((d, True), 0) for d in ("REVIEW", "DENY"))
    flagged_legit = sum(outcome.get((d, False), 0) for d in ("REVIEW", "DENY"))

    hits: Counter[str] = Counter()
    names: dict[str, str] = {}
    for fired in rule_rows:
        for hit in fired:
            hits[hit["id"]] += 1
            names[hit["id"]] = hit["name"]

    days: dict[str, dict[str, int]] = {}
    for d, decision, n in daily:
        days.setdefault(str(d), {"ALLOW": 0, "REVIEW": 0, "DENY": 0})[decision] = n

    model = request.app.state.models.current
    return {
        "decisions": {
            "total": sum(by_decision.values()),
            **{k: by_decision.get(k, 0) for k in ("ALLOW", "REVIEW", "DENY")},
        },
        "decided_by": {k: by_source.get(k, 0) for k in ("rule", "model", "default")},
        "review_queue": queue,
        "analyst": {
            "labelled": sum(labelled.values()),
            "confirmed_fraud": sum(confirmed.values()),
            "confirmed_rate": {
                k: _rate(confirmed[k], labelled[k]) for k in ("REVIEW", "DENY", "ALLOW")
            },
            "agreement_with_ground_truth": _rate(agree, compared),
        },
        "ground_truth": {
            "labelled_events": fraud_total + legit_total,
            "fraud_events": fraud_total,
            "recall": _rate(flagged_fraud, fraud_total),
            "false_positive_rate": _rate(flagged_legit, legit_total),
            "precision": _rate(flagged_fraud, flagged_fraud + flagged_legit),
        }
        if fraud_total + legit_total
        else None,
        "rule_hits": [
            {"id": rule_id, "name": names[rule_id], "hits": n}
            for rule_id, n in sorted(hits.items())
        ],
        "score_histogram": [
            {
                "from": 10 * b,
                "to": 10 * b + 10,
                "count": histogram.get(b, 0) + (histogram.get(10, 0) if b == 9 else 0),
            }
            for b in range(10)
        ],
        "by_day": [{"day": d, **counts} for d, counts in sorted(days.items())],
        "latency_ms": {
            "processing": percentiles([r.latency_ms for r in recent]),
            "end_to_end": percentiles(
                [r.latency_ms + r.stage_ms.get("queue", 0.0) for r in recent]
            ),
        },
        "model": {
            "version": model.version,
            "kind": model.metadata.get("kind"),
            "auc_roc": model.metadata.get("metrics", {}).get("auc_roc"),
            "pr_auc": model.metadata.get("metrics", {}).get("pr_auc"),
        }
        if model
        else None,
    }
