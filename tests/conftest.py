from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

from fraud_engine.config import Settings
from fraud_engine.main import create_app

STREAM = "events:test"


@pytest.fixture
async def redis():
    client = FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def app(tmp_path, redis):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        stream_name=STREAM,
        # Never pick up a model trained in the working copy.
        model_dir=str(tmp_path / "models"),
    )
    application = create_app(settings, redis=redis)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _ts(minutes_ago: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def payment():
    def make(**overrides) -> dict:
        event = {
            "event_id": str(uuid4()),
            "event_type": "payment",
            "user_id": "usr_alice01",
            "card_id": "tok_a1b2c3d4e5f6",
            "device_id": "dev_phone01",
            "ip_address": "49.37.12.8",
            "geo": {"lat": 12.97, "lon": 77.59, "country": "IN", "city": "Bengaluru"},
            "timestamp": _ts(),
            "amount": "1499.00",
            "currency": "INR",
            "merchant_id": "mer_grocery_001",
            "merchant_category": "grocery",
            "channel": "card_present",
            "status": "approved",
        }
        event.update(overrides)
        return event

    return make


@pytest.fixture
def instrument():
    def make(**overrides) -> dict:
        event = {
            "event_id": str(uuid4()),
            "event_type": "instrument",
            "user_id": "usr_alice01",
            "card_id": "tok_newcard9z8y",
            "device_id": "dev_phone01",
            "ip_address": "49.37.12.8",
            "timestamp": _ts(10),
            "action": "added",
            "bin": "457173",
            "card_type": "credit",
            "issuing_country": "IN",
            "name_matches_account": True,
        }
        event.update(overrides)
        return event

    return make
