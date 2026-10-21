import asyncio
import os
import time
from datetime import UTC, datetime

import numpy as np
import pytest
from fakeredis.aioredis import FakeRedis
from prometheus_client import REGISTRY
from sqlalchemy import select, update

from fraud_engine.audit import verify_decisions
from fraud_engine.db import DecisionRecord, RulesetRecord
from fraud_engine.features import FeatureEngine
from fraud_engine.generator import generate_dataset
from fraud_engine.ml import ModelRegistry
from fraud_engine.ml.dataset import TrainingSet
from fraud_engine.ml.train import save, train_xgboost
from fraud_engine.offline import build_features
from fraud_engine.pipeline import Pipeline
from fraud_engine.reporting import latency_report, percentiles
from fraud_engine.rules import RulesEngine, RuleSet
from fraud_engine.rules.engine import default_rules_text
from fraud_engine.stream import EventConsumer
from fraud_engine.worker import Worker, entry_time_ms

from .conftest import STREAM

END = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def small_model():
    ds = generate_dataset(users=80, days=14, seed=9, end=END)

    async def rows():
        client = FakeRedis()
        out = await build_features(ds.events, client)
        await client.aclose()
        return out

    feature_rows = asyncio.run(rows())
    labels = {lbl["event_id"]: lbl for lbl in ds.labels}
    ts = TrainingSet(
        vectors=[r["features"] for r in feature_rows],
        y=np.asarray([int(labels[r["event_id"]]["is_fraud"]) for r in feature_rows], np.int8),
        event_ids=[r["event_id"] for r in feature_rows],
        patterns=[labels[r["event_id"]]["pattern"] for r in feature_rows],
    )
    ruleset = RuleSet.from_yaml(default_rules_text())
    return train_xgboost(ts, "xgb-audit", ruleset, latency_sample=50), ds


def _worker(app, redis, rules: RulesEngine | None = None) -> Worker:
    model_dir = app.state.models.model_dir
    return Worker(
        db=app.state.db,
        consumer=EventConsumer(redis, STREAM, "audit-group", "w1"),
        pipeline=Pipeline(FeatureEngine(redis), rules, ModelRegistry(model_dir, 0)),
        deadletter_stream="dlq",
        batch_size=200,
        block_ms=10,
    )


async def _replay(client, events):
    for i in range(0, len(events), 500):
        resp = await client.post("/events/batch", json={"events": events[i : i + 500]})
        assert resp.status_code == 202


async def test_every_decision_is_reconstructable_and_tampering_is_caught(
    client, app, redis, small_model
):
    (model, report), ds = small_model
    model_dir = app.state.models.model_dir
    save(model, report, model_dir)
    ModelRegistry.promote(model_dir, "xgb-audit")

    # Recent events only: the API refuses timestamps in the future, and END is in the past.
    await _replay(client, ds.events[-600:])
    worker = _worker(app, redis)
    await worker.consumer.ensure_group()
    while await worker.run_once():
        pass

    result = await verify_decisions(app.state.db, model_dir)
    assert result["decisions"] == 600
    assert result["verified"] == result["matched"] == 600
    assert result["models"] == ["xgb-audit"] and len(result["rulesets"]) == 1

    async with app.state.db.sessionmaker() as session:
        victim = (await session.scalars(select(DecisionRecord.event_id).limit(1))).one()
        await session.execute(
            update(DecisionRecord)
            .where(DecisionRecord.event_id == victim)
            .values(final_decision="ALLOW", decided_by="default", ml_score=0.0)
        )
        await session.commit()
    result = await verify_decisions(app.state.db, model_dir)
    assert result["mismatched"] >= 1
    assert result["mismatch_examples"][0]["event_id"] == str(victim)


async def test_decisions_without_their_model_are_reported_unverifiable(
    client, app, redis, small_model, tmp_path
):
    (model, report), ds = small_model
    model_dir = app.state.models.model_dir
    save(model, report, model_dir)
    ModelRegistry.promote(model_dir, "xgb-audit")
    await _replay(client, ds.events[-50:])
    worker = _worker(app, redis)
    await worker.consumer.ensure_group()
    await worker.run_once()
    result = await verify_decisions(app.state.db, tmp_path / "elsewhere")
    assert result["verified"] == 0
    assert result["unverifiable"] == {"model not on disk": 50}


async def test_each_ruleset_version_used_is_stored(client, app, redis, tmp_path, payment):
    rules_file = tmp_path / "rules.yml"
    rules_file.write_text(default_rules_text())
    engine = RulesEngine(rules_file, check_interval=0)
    worker = _worker(app, redis, engine)
    await worker.consumer.ensure_group()

    await client.post("/events/payment", json=payment())
    await worker.run_once()
    rules_file.write_text(default_rules_text() + "\n# tuned\n")
    stamp = time.time() + 2
    os.utime(rules_file, (stamp, stamp))
    await client.post("/events/payment", json=payment())
    await worker.run_once()

    async with app.state.db.sessionmaker() as session:
        versions = set((await session.scalars(select(RulesetRecord.version))).all())
        used = set((await session.scalars(select(DecisionRecord.ruleset_version))).all())
    assert len(versions) == 2 and versions == used


async def test_queue_time_thresholds_and_latency_report(client, app, redis, payment):
    worker = _worker(app, redis)
    await worker.consumer.ensure_group()
    for _ in range(3):
        await client.post("/events/payment", json=payment())
    await worker.run_once()
    async with app.state.db.sessionmaker() as session:
        rows = (await session.scalars(select(DecisionRecord))).all()
    assert all(r.stage_ms["queue"] >= 0 for r in rows)
    assert all(r.thresholds == {"review_at": 40.0, "deny_above": 75.0} for r in rows)
    report = await latency_report(app.state.db)
    assert report["decisions"] == 3
    assert set(report["stages_ms"]) == {"features", "rules", "ml", "decision"}
    assert report["end_to_end_ms"]["p50"] >= report["processing_ms"]["p50"]


def test_entry_time_and_percentiles():
    assert entry_time_ms("1729416600000-3") == 1729416600000.0
    assert percentiles([]) == {}
    assert percentiles([2.0])["p95"] == 2.0
    assert percentiles([float(i) for i in range(1, 101)])["p50"] == 50.5


async def test_metrics_exposed_by_api_and_counted_by_worker(client, app, redis, payment):
    before = (
        REGISTRY.get_sample_value("fraud_events_processed_total", {"event_type": "payment"}) or 0
    )
    await client.post("/events/payment", json=payment())
    worker = _worker(app, redis)
    await worker.consumer.ensure_group()
    await worker.run_once()
    after = REGISTRY.get_sample_value("fraud_events_processed_total", {"event_type": "payment"})
    assert after == before + 1

    resp = await client.get("/metrics/")
    assert resp.status_code == 200
    assert 'fraud_ingest_events_total{event_type="payment",outcome="accepted"}' in resp.text
    assert "fraud_stage_seconds_bucket" in resp.text
