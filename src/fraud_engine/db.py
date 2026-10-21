"""Persistence for raw events.

The `events` table is the source of truth: every accepted event lands here before it is
published to the stream, so the pipeline can always be replayed from Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Insert, Integer, String, Uuid, func, select
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


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_tables(self) -> None:
        # Fine for Milestone 1. Switch to Alembic migrations once the schema starts changing.
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def insert_ignore(self, model: type[Base], rows: list[dict]) -> Insert:
        """INSERT ... ON CONFLICT DO NOTHING, so a retried batch doesn't fail on rows it
        already wrote."""
        dialect = postgresql if self.engine.dialect.name == "postgresql" else sqlite
        return dialect.insert(model).values(rows).on_conflict_do_nothing()

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
