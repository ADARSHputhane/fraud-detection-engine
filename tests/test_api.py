import json
from uuid import uuid4

from redis.exceptions import ConnectionError as RedisConnectionError

from .conftest import STREAM

VISA_TEST_PAN = "4111111111111111"


async def test_payment_accepted_persisted_and_streamed(client, redis, payment):
    event = payment()
    resp = await client.post("/events/payment", json=event)
    assert resp.status_code == 202
    body = resp.json()
    assert body["event_id"] == event["event_id"]
    assert body["status"] == "accepted"

    stored = await client.get(f"/events/{event['event_id']}")
    assert stored.status_code == 200
    assert stored.json()["payload"]["amount"] == "1499.00"

    entries = await redis.xrange(STREAM)
    assert len(entries) == 1
    stream_id, fields = entries[0]
    assert stream_id.decode() == body["stream_id"]
    assert fields[b"event_type"] == b"payment"
    assert json.loads(fields[b"data"])["event_id"] == event["event_id"]


async def test_instrument_accepted(client, redis, instrument):
    resp = await client.post("/events/instrument", json=instrument())
    assert resp.status_code == 202
    entries = await redis.xrange(STREAM)
    assert entries[0][1][b"event_type"] == b"instrument"


async def test_duplicate_event_rejected(client, redis, payment):
    event = payment()
    assert (await client.post("/events/payment", json=event)).status_code == 202
    resp = await client.post("/events/payment", json=event)
    assert resp.status_code == 409
    assert await redis.xlen(STREAM) == 1  # not published twice


async def test_malformed_event_gives_clear_field_errors(client, payment):
    resp = await client.post(
        "/events/payment", json=payment(amount="-5", currency="rupees", ip_address="nope")
    )
    assert resp.status_code == 422
    fields = {e["field"] for e in resp.json()["detail"]}
    assert {"amount", "currency", "ip_address"} <= fields


async def test_raw_card_number_rejected_and_never_echoed(client, payment):
    resp = await client.post("/events/payment", json=payment(card_id=VISA_TEST_PAN))
    assert resp.status_code == 422
    assert "raw card number" in resp.text
    assert VISA_TEST_PAN not in resp.text


async def test_wrong_event_type_on_endpoint_rejected(client, instrument):
    resp = await client.post("/events/payment", json=instrument())
    assert resp.status_code == 422


async def test_batch_mixed_in_order_with_duplicates(client, redis, payment, instrument):
    already_sent = payment()
    await client.post("/events/payment", json=already_sent)

    card_added = instrument()
    first_use = payment(card_id=card_added["card_id"])
    events = [card_added, first_use, first_use, already_sent]
    resp = await client.post("/events/batch", json={"events": events})
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 2
    assert sorted(body["duplicates"]) == sorted([first_use["event_id"], already_sent["event_id"]])

    entries = await redis.xrange(STREAM)
    order = [json.loads(f[b"data"])["event_id"] for _, f in entries]
    assert order == [already_sent["event_id"], card_added["event_id"], first_use["event_id"]]


async def test_batch_rejected_whole_if_any_event_invalid(client, redis, payment):
    good, bad = payment(), payment(amount="0")
    resp = await client.post("/events/batch", json={"events": [good, bad]})
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["field"].startswith("events.1")
    assert (await client.get(f"/events/{good['event_id']}")).status_code == 404
    assert await redis.xlen(STREAM) == 0


async def test_batch_size_limits(client, payment):
    assert (await client.post("/events/batch", json={"events": []})).status_code == 422
    too_many = [payment() for _ in range(1001)]
    assert (await client.post("/events/batch", json={"events": too_many})).status_code == 422


async def test_get_unknown_event_404(client):
    assert (await client.get(f"/events/{uuid4()}")).status_code == 404


async def test_health_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "postgres": True, "redis": True}


async def test_health_degraded_when_redis_down(client, app, monkeypatch):
    async def down():
        raise RedisConnectionError("down")

    monkeypatch.setattr(app.state.stream.redis, "ping", down)
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["redis"] is False


async def test_stream_failure_rolls_back_so_retry_succeeds(client, app, payment, monkeypatch):
    event = payment()

    async def broken(*_args, **_kwargs):
        raise RedisConnectionError("down")

    with monkeypatch.context() as m:
        m.setattr(app.state.stream, "publish", broken)
        resp = await client.post("/events/payment", json=event)
    assert resp.status_code == 503
    assert (await client.get(f"/events/{event['event_id']}")).status_code == 404

    retry = await client.post("/events/payment", json=event)
    assert retry.status_code == 202


async def test_batch_stream_failure_rolls_back(client, app, payment, monkeypatch):
    events = [payment(), payment()]

    async def broken(*_args, **_kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(app.state.stream, "publish_many", broken)
    resp = await client.post("/events/batch", json={"events": events})
    assert resp.status_code == 503
    for e in events:
        assert (await client.get(f"/events/{e['event_id']}")).status_code == 404
