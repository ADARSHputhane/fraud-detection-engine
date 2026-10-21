import json

from fraud_engine.features import FeatureEngine
from fraud_engine.pipeline import Pipeline
from fraud_engine.stream import EventConsumer
from fraud_engine.worker import Worker

from .conftest import STREAM

DLQ = "events:test:dlq"


def make_worker(app, redis) -> Worker:
    return Worker(
        db=app.state.db,
        consumer=EventConsumer(redis, STREAM, "test-scorers", "worker-1"),
        pipeline=Pipeline(FeatureEngine(redis, prefix="fe-test")),
        deadletter_stream=DLQ,
        batch_size=50,
        block_ms=10,
    )


async def test_worker_computes_and_serves_features(client, app, redis, instrument, payment):
    card_added = instrument()
    first_use = payment(card_id=card_added["card_id"], amount="25000.00")
    await client.post("/events/batch", json={"events": [card_added, first_use]})

    worker = make_worker(app, redis)
    await worker.consumer.ensure_group()
    assert await worker.run_once() == 2

    resp = await client.get(f"/features/{first_use['event_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["feature_version"] == 1
    assert body["features"]["card_age_hours"] is not None
    assert body["features"]["new_card_high_amount"] is True
    # Everything was acknowledged.
    assert (await redis.xpending(STREAM, "test-scorers"))["pending"] == 0


async def test_features_404_before_processing(client, payment):
    event = payment()
    await client.post("/events/payment", json=event)
    resp = await client.get(f"/features/{event['event_id']}")
    assert resp.status_code == 404
    assert "worker" in resp.json()["detail"]


async def test_bad_entry_goes_to_dead_letter_and_stream_keeps_moving(app, redis, payment):
    await redis.xadd(STREAM, {"event_type": "payment", "event_id": "x", "data": "{not json"})
    await redis.xadd(
        STREAM, {"event_type": "payment", "event_id": "y", "data": json.dumps(payment())}
    )
    worker = make_worker(app, redis)
    await worker.consumer.ensure_group()
    await worker.run_once()
    assert worker.processed == 1 and worker.failed == 1
    dead = await redis.xrange(DLQ)
    assert len(dead) == 1 and b"JSONDecodeError" in dead[0][1][b"error"]
    assert (await redis.xpending(STREAM, "test-scorers"))["pending"] == 0


async def test_unacknowledged_entries_are_recovered(client, app, redis, payment):
    events = [payment(), payment()]
    await client.post("/events/batch", json={"events": events})
    worker = make_worker(app, redis)
    await worker.consumer.ensure_group()
    # Simulate a crash: entries delivered to this consumer but never processed or acked.
    assert len(await worker.consumer.read(10, block_ms=10)) == 2
    assert (await redis.xpending(STREAM, "test-scorers"))["pending"] == 2

    await worker.recover_pending()
    assert (await redis.xpending(STREAM, "test-scorers"))["pending"] == 0
    for e in events:
        assert (await client.get(f"/features/{e['event_id']}")).status_code == 200


async def test_reprocessing_a_batch_is_safe(app, redis, payment):
    await redis.xadd(
        STREAM, {"event_type": "payment", "event_id": "z", "data": json.dumps(payment())}
    )
    worker = make_worker(app, redis)
    await worker.consumer.ensure_group()
    entries = await worker.consumer.read(10, block_ms=10)
    first = await worker.handle(entries)
    again = await worker.handle(entries)  # e.g. the ack was lost
    assert first[0].features == again[0].features


async def test_features_are_returned_in_catalogue_order(client, app, redis, payment):
    from fraud_engine.features import FEATURE_NAMES

    event = payment()
    await client.post("/events/payment", json=event)
    worker = make_worker(app, redis)
    await worker.consumer.ensure_group()
    await worker.run_once()
    body = (await client.get(f"/features/{event['event_id']}")).json()
    assert tuple(body["features"]) == FEATURE_NAMES
