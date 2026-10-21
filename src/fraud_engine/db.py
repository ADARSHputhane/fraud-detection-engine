"""Persistence for raw events.

The `events` table is the source of truth: every accepted event lands here before it is
published to the stream, so the pipeline can always be replayed from Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Insert,
    Integer,
    String,
    Text,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# JSONB on Postgres (indexable, compact); plain JSON elsewhere (SQLite in tests).
JSONPayload = JSON().with_variant(JSONB(), "postgresql")


class EventRecord(Base):
    __tablename__ = "events"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str] = mapped_column(String(64))
    card_id: Mapped[str] = mapped_column(String(68))
    device_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    payload: Mapped[dict] = mapped_column(JSONPayload)

    # The feature engine looks up history per entity, ordered by event time.
    __table_args__ = (
        Index("ix_events_user_time", "user_id", "occurred_at"),
        Index("ix_events_card_time", "card_id", "occurred_at"),
        Index("ix_events_device_time", "device_id", "occurred_at"),
    )


class FeatureRecord(Base):
    """The feature vector computed for an event — what the rules and the model saw."""

    __tablename__ = "features"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    feature_version: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    vector: Mapped[dict] = mapped_column(JSONPayload)


class DecisionRecord(Base):
    """The audit log: what was decided for each event, by which rule or model, and how fast.

    Together with the stored feature vector, the ruleset version and the model version, a row
    is enough to recompute the decision and check it.
    """

    __tablename__ = "decisions"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    final_decision: Mapped[str] = mapped_column(String(8))
    # "rule:R001", "model" or "default": the stage that set the final decision.
    decided_by: Mapped[str] = mapped_column(String(32))
    rules_fired: Mapped[list] = mapped_column(JSONPayload)
    rule_score: Mapped[int] = mapped_column(Integer)
    ruleset_version: Mapped[str] = mapped_column(String(16))
    ml_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shap_top5: Mapped[list | None] = mapped_column(JSONPayload, nullable=True)
    # The score bands in force, so the decision can be recomputed after they change.
    thresholds: Mapped[dict] = mapped_column(JSONPayload)
    # Processing time (features + rules + model + decision); stage_ms also holds "queue",
    # the wait between the API publishing the event and the worker picking it up.
    latency_ms: Mapped[float] = mapped_column(Float)
    stage_ms: Mapped[dict] = mapped_column(JSONPayload)

    __table_args__ = (
        Index("ix_decisions_decision_time", "final_decision", "decided_at"),
        Index("ix_decisions_time", "decided_at"),
        # The case view shows the user's recent history next to the event under review.
        Index("ix_decisions_user_time", "user_id", "occurred_at"),
    )


class RulesetRecord(Base):
    """Every ruleset version that made a decision, so old decisions can be re-run."""

    __tablename__ = "rulesets"

    version: Mapped[str] = mapped_column(String(16), primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    first_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LabelRecord(Base):
    """Labels for training, one per event per source.

    "synthetic" is the generator's ground truth (in production: chargebacks and confirmed
    fraud reports); "analyst" is a verdict from the review dashboard. Training prefers the
    analyst's label. Keeping both lets the stats page show how often analysts agree with
    ground truth, instead of the analyst's verdict silently overwriting it.
    """

    __tablename__ = "labels"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    is_fraud: Mapped[bool] = mapped_column(Boolean)
    pattern: Mapped[str | None] = mapped_column(String(32), nullable=True)
    analyst: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    labelled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        # create_all adds missing tables but never alters existing ones. That holds while each
        # milestone only adds tables; switch to Alembic migrations before changing a column.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def insert_ignore(self, model: type[Base], rows: list[dict]) -> Insert:
        """INSERT ... ON CONFLICT DO NOTHING, so a retried batch doesn't fail on rows it
        already wrote."""
        dialect = postgresql if self.engine.dialect.name == "postgresql" else sqlite
        return dialect.insert(model).values(rows).on_conflict_do_nothing()

    def upsert(self, model: type[Base], rows: list[dict], keys: list[str]) -> Insert:
        """INSERT ... ON CONFLICT (keys) DO UPDATE, setting every non-key column."""
        dialect = postgresql if self.engine.dialect.name == "postgresql" else sqlite
        stmt = dialect.insert(model).values(rows)
        columns = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
        return stmt.on_conflict_do_update(index_elements=keys, set_=columns)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(select(1))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
