"""FastAPI application factory.

Run locally:  uvicorn fraud_engine.main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from redis.asyncio import Redis

from . import __version__
from .api import router
from .config import Settings, get_settings
from .db import Database
from .ml import ModelRegistry
from .review_api import router as review_router
from .rules import RulesEngine
from .scoring_api import router as scoring_router
from .stream import EventStream


async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return field-level messages WITHOUT echoing the submitted values.

    FastAPI's default 422 body includes the rejected input. If a client mistakenly sends a
    raw card number, echoing it back would leak it into client logs, proxies and traces.
    """
    errors = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=422)


def create_app(settings: Settings | None = None, redis: Redis | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake Redis client."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        await db.create_tables()
        client = redis or Redis.from_url(settings.redis_url)
        app.state.db = db
        app.state.stream = EventStream(client, settings.stream_name, settings.stream_maxlen)
        app.state.settings = settings
        app.state.models = ModelRegistry(Path(settings.model_dir), settings.model_reload_seconds)
        app.state.rules = RulesEngine(
            Path(settings.rules_path) if settings.rules_path else None,
            settings.rules_reload_seconds,
        )
        try:
            yield
        finally:
            await db.dispose()
            if redis is None:
                await client.aclose()

    app = FastAPI(
        title="Payment Fraud Detection Engine",
        version=__version__,
        description="Real-time ingestion and risk scoring of payment events.",
        lifespan=lifespan,
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.include_router(router)
    app.include_router(scoring_router)
    app.include_router(review_router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
