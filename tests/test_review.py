from uuid import UUID, uuid4

import pytest

from fraud_engine.db import LabelRecord
from fraud_engine.features import FEATURES, FeatureEngine
from fraud_engine.pipeline import Pipeline
from fraud_engine.stream import EventConsumer
from fraud_engine.worker import Worker

from .conftest import STREAM


@pytest.fixture
async def scored(client, app, redis, payment, instrument):
    """A small scored world: a card-testing ring (DENY), hosting-IP payments from new
    devices (REVIEW) and a normal customer's purchases (ALLOW)."""
    ring = [
        instrument(user_id="usr_ring01", device_id="dev_ring01", card_id=f"tok_ring{i:06d}")
        for i in range(3)
    ]
    reviews = [
        payment(user_id=f"usr_vpn{i:02d}", device_id=f"dev_vpn{i:02d}", ip_address="54.10.0.1")
        for i in range(3)
    ]
    normal = [payment(user_id="usr_alice01", amount=f"{100 + i}.00") for i in range(4)]
    events = ring + reviews + normal
    assert (await client.post("/events/batch", json={"events": events})).status_code == 202
    worker = Worker(
        db=app.state.db,
        consumer=EventConsumer(redis, STREAM, "review-tests", "w1"),
        pipeline=Pipeline(FeatureEngine(redis)),
        deadletter_stream="dlq",
    )
    await worker.consumer.ensure_group()
    await worker.run_once()
    return {"ring": ring, "reviews": reviews, "normal": normal}


async def test_queue_lists_unlabelled_reviews_with_context(client, scored):
    body = (await client.get("/decisions")).json()
    assert body["total"] == 3
    item = body["items"][0]
    assert item["final_decision"] == "REVIEW"
    assert item["amount"] == "1499.00" and item["merchant_category"] == "grocery"
    assert item["top_rule"]["id"] == "R006"
    assert item["analyst_label"] is None

    flagged = (await client.get("/decisions", params={"view": "flagged"})).json()
    assert flagged["total"] == 4  # three reviews + the third card added by the ring
    page = (await client.get("/decisions", params={"view": "all", "limit": 2})).json()
    assert page["total"] == 10 and len(page["items"]) == 2


async def test_labelling_moves_an_event_out_of_the_queue_and_back(client, scored):
    event_id = scored["reviews"][0]["event_id"]
    resp = await client.post(
        "/labels", json={"event_id": event_id, "is_fraud": True, "analyst": "asha", "note": "VPN"}
    )
    assert resp.status_code == 201
    assert resp.json()["source"] == "analyst" and resp.json()["is_fraud"] is True

    queue = (await client.get("/decisions")).json()
    assert event_id not in {i["event_id"] for i in queue["items"]} and queue["total"] == 2
    done = (await client.get("/decisions", params={"labelled": "yes"})).json()
    assert done["items"][0]["analyst_label"] is True

    # Changing your mind replaces the verdict rather than adding a second one.
    await client.post("/labels", json={"event_id": event_id, "is_fraud": False})
    case = (await client.get(f"/cases/{event_id}")).json()
    assert case["label"]["is_fraud"] is False and case["label"]["analyst"] == "analyst"

    assert (await client.delete(f"/labels/{event_id}")).status_code == 204
    assert (await client.get("/decisions")).json()["total"] == 3


async def test_label_validation(client, scored):
    unknown = await client.post("/labels", json={"event_id": str(uuid4()), "is_fraud": True})
    assert unknown.status_code == 404
    event_id = scored["reviews"][0]["event_id"]
    bad = await client.post(
        "/labels", json={"event_id": event_id, "is_fraud": True, "analyst": "<script>"}
    )
    assert bad.status_code == 422
    long_note = await client.post(
        "/labels", json={"event_id": event_id, "is_fraud": True, "note": "x" * 501}
    )
    assert long_note.status_code == 422


async def test_case_view_has_everything_and_never_leaks_ground_truth(client, app, scored):
    target = scored["normal"][-1]
    async with app.state.db.sessionmaker() as session:
        session.add(
            LabelRecord(event_id=UUID(target["event_id"]), source="synthetic", is_fraud=True)
        )
        await session.commit()

    case = (await client.get(f"/cases/{target['event_id']}")).json()
    assert case["event"]["amount"] == target["amount"]
    assert case["decision"]["final_decision"] == "ALLOW"
    assert [f["name"] for f in case["features"]] == [f.name for f in FEATURES]
    assert all(f["description"] for f in case["features"])
    assert case["label"] is None  # the synthetic label exists but is not shown
    assert [h["amount"] for h in case["history"]] == ["102.00", "101.00", "100.00"]
    assert (await client.get(f"/cases/{uuid4()}")).status_code == 404


async def test_stats(client, app, scored):
    reviews = scored["reviews"]
    async with app.state.db.sessionmaker() as session:
        for e in reviews:
            session.add(
                LabelRecord(event_id=UUID(e["event_id"]), source="synthetic", is_fraud=True)
            )
        await session.commit()
    await client.post("/labels", json={"event_id": reviews[0]["event_id"], "is_fraud": True})
    await client.post("/labels", json={"event_id": reviews[1]["event_id"], "is_fraud": False})

    s = (await client.get("/stats")).json()
    assert s["decisions"] == {"total": 10, "ALLOW": 6, "REVIEW": 3, "DENY": 1}
    assert s["decided_by"]["rule"] == 4 and s["review_queue"] == 1
    assert s["analyst"]["labelled"] == 2 and s["analyst"]["confirmed_rate"]["REVIEW"] == 0.5
    assert s["analyst"]["agreement_with_ground_truth"] == 0.5
    assert s["ground_truth"]["recall"] == 1.0
    assert {r["id"] for r in s["rule_hits"]} >= {"R001", "R006"}
    assert len(s["score_histogram"]) == 10
    assert sum(d["ALLOW"] + d["REVIEW"] + d["DENY"] for d in s["by_day"]) == 10
    assert s["latency_ms"]["processing"]["p50"] > 0
    assert s["model"] is None
